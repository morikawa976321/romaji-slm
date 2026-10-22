"""常駐の変換サーバ（docs/IME.md）。トレイアプリ ime/RomajiSlmIme.cs が 127.0.0.1 で呼ぶ。

    uv run python -m ime.server [--port 8767] [--device auto|cuda|cpu] [--engine auto|torch|onnx] [--threads 4]

POST /convert[?mode=first|full][&prefetch=1]
               本文（UTF-8）: 1 行目 = 打鍵列、2 行目以降 = 左文脈
               応答（UTF-8）: 1 行目 = 変換にかかった ms、2 行目以降 = 候補（1 位が既定の変換）
               mode=first は 1 位と読みの候補だけで返し、そのあと暇なうちに候補ウィンドウの別の書き方を足しておく。
               mode=full（既定）は別の書き方まで揃った列（core.window.nbest と同じ）。1 位はどちらも同じ。
               prefetch=1 の要求は、後から別の要求が来たら forward の切れ目でやめて 409 を返す（打っている途中の先読み。
               確定の要求には付けない）。docs/decisions.md D47
GET  /health   "ok <版>"（読み込みが終わるまで応答しない）

入力の文字列はログにも標準出力にも残さない（パスワード欄で誤って押された打鍵も来うる）。
C# 側で JSON を組まずに済むよう、行区切りの素の文字列にしてある。
"""

import argparse
import os
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core.cancel import Cancelled, interrupt, scope

DEFAULT_PORT = 8767  # 体験版（trial/server.py）は 8765、その MockEngine 版（.claude/launch.json）は 8766
ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    p = ROOT / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "dev"


def thread_env(n: int) -> None:
    """torch（Intel OpenMP・MKL）の CPU のスレッド数を、torch を読む前に環境変数で渡す（読み込み時にスレッドの池を作る部分が
    あるため。ime.ini の値を利用者の環境変数より優先する）。torch を読むのは torch 版（GPU）と ONNX の初回の書き出しだけ。"""
    if n > 0:
        for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[k] = str(n)


def set_threads(n: int) -> None:
    if n > 0:
        import torch
        torch.set_num_threads(n)


def pick_engine(engine: str, device: str) -> str:
    """torch 版は GPU（cuda）だけで動かし、CPU では ONNX Runtime 版を使う（torch の CPU の経路は削除した。docs/decisions.md D44）。
    auto と torch は GPU があれば torch、無ければ onnx。onnx は GPU があっても ONNX Runtime 版（CPU）。"""
    return "torch" if engine != "onnx" and device == "cuda" else "onnx"


def has_cuda_driver() -> bool:
    """NVIDIA のドライバで CUDA の装置が 1 つ以上使えるか。torch を読まずに見る（GPU の無い機械で torch を読むと、
    ONNX 版の常駐の専有が約 950 MB 重くなる。docs/RESOURCES.md「減らす手の実測」）。torch が CUDA を使えるかは別で、main で確かめる。"""
    import ctypes
    try:
        lib = ctypes.WinDLL("nvcuda.dll") if os.name == "nt" else ctypes.CDLL("libcuda.so.1")
    except OSError:
        return False
    n = ctypes.c_int(0)
    return lib.cuInit(0) == 0 and lib.cuDeviceGetCount(ctypes.byref(n)) == 0 and n.value > 0


def pick_device(device: str) -> str:
    if device != "auto":
        return device
    return "cuda" if has_cuda_driver() else "cpu"


def parse_request(body: bytes) -> tuple[str, str]:
    text = body.decode("utf-8")
    romaji, _, ctx = text.partition("\n")
    return romaji.strip(), ctx.replace("\r", "").replace("\n", "")


def format_response(ms: float, cands: list[str]) -> bytes:
    lines = [f"{ms:.0f}"] + [c.replace("\r", "").replace("\n", "") for c in cands]
    return "\n".join(lines).encode("utf-8")


class Lists:
    """常駐入力の候補の列。打鍵列と左文脈ごとに、候補の集まり（core.window.Pool）を最近の分だけ取っておく。
    first は 1 位と読みの候補だけ、full は別の書き方を足した列で、full は core.window.nbest と同じ（D47）。"""

    KEEP = 16

    def __init__(self, p, order: str):
        self.p, self.order = p, order
        self.pools: OrderedDict = OrderedDict()

    def clear(self) -> None:
        self.pools.clear()

    def _pool(self, romaji: str, ctx: str):
        from core import window
        k = (romaji, ctx)
        if k in self.pools:
            self.pools.move_to_end(k)
            return self.pools[k]
        pl = window.pool(self.p, romaji, ctx, alts=False)
        self.pools[k] = pl
        while len(self.pools) > self.KEEP:
            self.pools.popitem(last=False)
        return pl

    def first(self, romaji: str, ctx: str) -> list[str]:
        from core import window
        pl = self._pool(romaji, ctx)
        return [] if pl is None else window.texts(pl, self.order)

    def full(self, romaji: str, ctx: str) -> list[str]:
        from core import window
        pl = self._pool(romaji, ctx)
        if pl is None:
            return []
        if self.order != "reading":  # reading の並べ方は別の書き方を使わない（nbest と同じ）
            window.add_alternatives(self.p, pl, ctx)
        return window.texts(pl, self.order)


# 要求がこの秒数来なければ、変換の中で広げたメモリを返す（release）。返した後の最初の要求は広げ直す分だけ遅い
# （このデスクトップの ONNX 版で中央値 +約 75 ms、torch 版は +10 ms 前後。2 秒で返すと、考えながら打つ間のたびに払う）ので、
# 席を離れたくらいの間だけにする。30 は AI が決めた目安（docs/decisions.md D50）
RELEASE_AFTER = 30.0


def make_handler(convert, ready: threading.Event, idle=None, release=None, release_after: float = RELEASE_AFTER):
    """convert(romaji, ctx, full) が候補の列を返す。idle(romaji, ctx) は mode=first の応答のあと、暇なうちに full の列を
    用意しておく（無ければしない）。変換は同時に 1 つだけ（錠）。prefetch の要求と暇なときの用意は、後から別の要求が
    来たらやめる（core.cancel。走っている forward も止める）。release() は、最後の要求から release_after 秒だれも
    来なかったときに、暇なときの用意の後で呼ぶ（無ければしない）。"""
    lock = threading.Lock()  # モデルを複数の要求で同時に動かさない
    newest = {"seq": 0, "key": None, "full": False}   # いちばん新しい要求
    seq_lock = threading.Lock()
    timer = {"t": None}
    timer_lock = threading.Lock()

    def schedule_release() -> None:
        if release is None:
            return
        my = newest["seq"]

        def fire():
            with lock:   # 暇なときの用意が走っていれば、その後で
                if newest["seq"] != my:   # その後に要求が来た。その要求の終わりにまた予約される
                    return
                try:
                    release()
                except Exception as e:
                    print(f"release: {type(e).__name__}", flush=True)

        with timer_lock:
            if timer["t"] is not None:
                timer["t"].cancel()
            t = timer["t"] = threading.Timer(release_after, fire)
            t.daemon = True
            t.start()

    def arrive(key, full: bool) -> int:
        with seq_lock:
            newest["seq"] += 1
            newest.update(key=key, full=full)
            my = newest["seq"]
        interrupt()   # もう要らなくなった変換の、走っている forward を止める
        return my

    def run_idle(my: int, romaji: str, ctx: str) -> None:
        # 後から来た要求があればやめる。ただし同じ打鍵列の full の要求なら、ここで揃えた列をそのまま使うので続ける
        def stale():
            return newest["seq"] != my and not (newest["full"] and newest["key"] == (romaji, ctx))
        try:
            with lock:
                if stale():
                    return
                with scope(stale):
                    idle(romaji, ctx)
        except Cancelled:
            pass
        except Exception as e:  # 用意できなくても、候補ウィンドウを開いたときに作り直す
            print(f"idle: {type(e).__name__}", flush=True)

    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # 要求を送らずに保持された接続で、他の要求と生存確認を止めない（接続ごとのスレッド＋読み取りの時間切れ）

        def log_message(self, fmt, *args):  # 既定は要求行（パス）を stderr に出すだけだが、念のため何も出さない
            pass

        def _send(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, b"not found")
            ready.wait()
            self._send(200, f"ok {version()}".encode())

        def do_POST(self):
            url = urlsplit(self.path)
            if url.path != "/convert":
                return self._send(404, b"not found")
            q = parse_qs(url.query)
            full = q.get("mode", ["full"])[0] != "first"
            prefetch = q.get("prefetch", ["0"])[0] == "1"
            n = int(self.headers.get("Content-Length") or 0)
            romaji, ctx = parse_request(self.rfile.read(n))
            if not romaji:
                return self._send(400, b"empty")
            my = arrive((romaji, ctx), full)
            stale = (lambda: newest["seq"] != my) if prefetch else None
            ready.wait()
            t0 = time.perf_counter()
            try:
                with lock:
                    if stale is not None and stale():
                        raise Cancelled
                    with scope(stale):
                        cands = convert(romaji, ctx, full)
            except Cancelled:
                return self._send(409, b"superseded")   # 後から来た要求の終わりに返す
            except Exception as e:  # 1 件の失敗で常駐を落とさない
                schedule_release()
                return self._send(500, f"{type(e).__name__}".encode())
            schedule_release()
            self._send(200, format_response((time.perf_counter() - t0) * 1000, cands))
            if not full and idle is not None and cands:
                threading.Thread(target=run_idle, args=(my, romaji, ctx), daemon=True).start()

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--model", default="Miwa-Keita/zenz-v3.1-small")
    # 候補ウィンドウの並べ方（core.window.ORDERS）。merge は本人の決定（docs/decisions.md D37）。reading で置き換える前に戻る
    ap.add_argument("--window", default="merge")
    ap.add_argument("--log", help="標準出力と標準エラーの行き先（pythonw では両方とも None で、進捗表示が落ちる）")
    # CPU のスレッド数（ime.ini の threads。既定 4、0 は実行系の既定 = 論理コア数）。ONNX Runtime 版の変換と、GPU のときの
    # torch の CPU 側に効く。既定のままだと、ほかの処理が CPU を使っているとき変換が極端に遅くなり、変換のたびに全コアを
    # 埋める（本人の決定 2026-10-19、docs/decisions.md D42）。スレッド数を変えても出力は変わらない
    ap.add_argument("--threads", type=int, default=4)
    # 実行方式（ime.ini の engine）。auto: GPU があれば torch、無ければ ONNX Runtime 版（出力は torch 版と同じ。
    # 本人の決定 2026-10-19、docs/decisions.md D43）。torch は GPU のときだけ（無ければ onnx で動く）、onnx は決め打ち
    ap.add_argument("--engine", choices=("auto", "torch", "onnx"), default="auto")
    args = ap.parse_args(argv)
    thread_env(args.threads)  # torch はまだ読まない（待ち受けを先に開くため）
    if args.log:
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
        print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} start", flush=True)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 常駐は起動のたびに Hugging Face へ問い合わせない（取得済みのキャッシュを使う）
    hf = ROOT / "models" / "hf"
    if (hf / "hub").exists():
        os.environ.setdefault("HF_HOME", str(hf))   # 配布 zip（tools/pack-ime.ps1）はモデルを同梱する

    ready = threading.Event()
    holder = {}

    def convert(romaji, ctx, full):
        reload_user_dict()
        lists = holder["lists"]
        return lists.full(romaji, ctx) if full else lists.first(romaji, ctx)

    def idle(romaji, ctx):
        holder["lists"].full(romaji, ctx)

    def reload_user_dict():
        """ユーザー辞書が更新されていたら読み直す（振り返りの後に再起動しなくてよいように）。変換の錠の中で呼ばれる"""
        from core.userdict import default_path, load as load_user
        p = default_path()
        m = p.stat().st_mtime if p.exists() else 0.0
        if m != holder.get("ud_mtime"):
            holder["ud_mtime"] = m
            ud = load_user(p)
            holder["p"].set_user_dict(ud)
            holder["lists"].clear()   # 取っておいた候補の列は前の辞書で作ったもの
            print(f"user dict reloaded: en={len(ud.en)} word={len(ud.words)} fix={len(ud.fixes)} errors={len(ud.errors)}", flush=True)

    # 先に待ち受けを開く。トレイアプリは /health が返るまで「準備中」と表示する
    srv = ThreadingHTTPServer(("127.0.0.1", args.port),
                              make_handler(convert, ready, idle, release=lambda: holder["p"].release_memory()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from core.pipeline import CorePipeline  # 既定は本人の選択（句読点は自動で足さない D48・英字は打ったまま D14。docs/decisions.md）
    device = pick_device(args.device)
    t0 = time.perf_counter()
    from core.userdict import default_path, load as load_user
    ud = load_user(default_path())   # 本人のユーザー辞書（docs/REVIEW.md）。中身はログに書かない（件数だけ）
    print(f"user dict: {default_path()} en={len(ud.en)} word={len(ud.words)} fix={len(ud.fixes)} errors={len(ud.errors)}", flush=True)
    engine = pick_engine(args.engine, device)
    if args.engine == "torch" and engine != "torch":
        print("engine=torch needs a GPU (cuda); using onnx", flush=True)
    if engine == "torch":
        import torch
        if not torch.cuda.is_available():   # ドライバはあるが torch が CUDA を使えない（CPU 版の torch など）
            print("torch cannot use cuda; using onnx", flush=True)
            device, engine = "cpu", "onnx"
    if engine == "onnx":
        try:
            from core.ort_backend import OrtCorePipeline
            holder["p"] = OrtCorePipeline(model=args.model, threads=args.threads or None, user_dict=ud)
        except Exception as e:
            # GPU があれば torch 版で動かす。無ければ変換できない（CPU の torch 版は削除した）ので、起動の失敗として記録に残す
            print(f"onnx engine failed: {type(e).__name__}: {e}", flush=True)
            if device != "cuda":
                raise
            engine = "torch"
    if engine == "torch":
        set_threads(args.threads)
        holder["p"] = CorePipeline(model=args.model, device=device, user_dict=ud)
    holder["ud_mtime"] = default_path().stat().st_mtime if default_path().exists() else 0.0
    holder["lists"] = Lists(holder["p"], args.window)
    holder["p"].convert_nbest("konnichiha", order=args.window)  # 最初の 1 件の遅さ（CUDA の初期化・辞書の索引）を起動時に済ませる
    ready.set()
    print(f"romaji-slm ime server: 127.0.0.1:{args.port} engine={engine} device={device if engine == 'torch' else 'cpu'} "
          f"threads={args.threads} ready in {time.perf_counter() - t0:.1f}s", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
