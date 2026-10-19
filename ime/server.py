"""常駐の変換サーバ（docs/IME.md）。トレイアプリ ime/RomajiSlmIme.cs が 127.0.0.1 で呼ぶ。

    uv run python -m ime.server [--port 8767] [--device auto|cuda|cpu]

POST /convert  本文（UTF-8）: 1 行目 = 打鍵列、2 行目以降 = 左文脈
               応答（UTF-8）: 1 行目 = 変換にかかった ms、2 行目以降 = 候補（1 位が既定の変換）
GET  /health   "ok <版>"（読み込みが終わるまで応答しない）

入力の文字列はログにも標準出力にも残さない（パスワード欄で誤って押された打鍵も来うる）。
C# 側で JSON を組まずに済むよう、行区切りの素の文字列にしてある。
"""

import argparse
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8767  # 体験版（trial/server.py）は 8765、その MockEngine 版（.claude/launch.json）は 8766
ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    p = ROOT / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "dev"


def thread_env(n: int) -> None:
    """torch（Intel OpenMP・MKL）のスレッド数を、torch を読む前に環境変数で渡す（読み込み時にスレッドの池を作る部分が
    あるため。ime.ini の値を利用者の環境変数より優先する）。torch を読んだ後に set_threads でも決める。"""
    if n > 0:
        for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[k] = str(n)


def set_threads(n: int) -> None:
    if n > 0:
        import torch
        torch.set_num_threads(n)


def pick_device(device: str) -> str:
    if device != "auto":
        return device
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def parse_request(body: bytes) -> tuple[str, str]:
    text = body.decode("utf-8")
    romaji, _, ctx = text.partition("\n")
    return romaji.strip(), ctx.replace("\r", "").replace("\n", "")


def format_response(ms: float, cands: list[str]) -> bytes:
    lines = [f"{ms:.0f}"] + [c.replace("\r", "").replace("\n", "") for c in cands]
    return "\n".join(lines).encode("utf-8")


def make_handler(convert, ready: threading.Event):
    lock = threading.Lock()  # torch のモデルを複数の要求で同時に動かさない

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
            if self.path != "/convert":
                return self._send(404, b"not found")
            n = int(self.headers.get("Content-Length") or 0)
            romaji, ctx = parse_request(self.rfile.read(n))
            if not romaji:
                return self._send(400, b"empty")
            ready.wait()
            t0 = time.perf_counter()
            try:
                with lock:
                    cands = convert(romaji, ctx)
            except Exception as e:  # 1 件の失敗で常駐を落とさない
                return self._send(500, f"{type(e).__name__}".encode())
            self._send(200, format_response((time.perf_counter() - t0) * 1000, cands))

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--model", default="Miwa-Keita/zenz-v3.1-small")
    # 候補ウィンドウの並べ方（core.window.ORDERS）。merge は本人の決定（docs/decisions.md D37）。reading で置き換える前に戻る
    ap.add_argument("--window", default="merge")
    ap.add_argument("--log", help="標準出力と標準エラーの行き先（pythonw では両方とも None で、進捗表示が落ちる）")
    # torch のスレッド数（ime.ini の threads。既定 4、0 は torch の既定 = 論理コア数）。既定のままだと、ほかの処理が CPU を
    # 使っているとき変換が極端に遅くなり（同じ負荷でスレッド 4 の 6 倍）、変換のたびに全コアを埋める（docs/LLAMA_PARITY.md）。
    # スレッド数を変えても出力は変わらない（全評価セット 896 文で確認）
    ap.add_argument("--threads", type=int, default=4)
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

    def convert(romaji, ctx):
        reload_user_dict()
        return holder["p"].convert_nbest(romaji, ctx, order=args.window)

    def reload_user_dict():
        """ユーザー辞書が更新されていたら読み直す（振り返りの後に再起動しなくてよいように）。変換の錠の中で呼ばれる"""
        from core.userdict import default_path, load as load_user
        p = default_path()
        m = p.stat().st_mtime if p.exists() else 0.0
        if m != holder.get("ud_mtime"):
            holder["ud_mtime"] = m
            ud = load_user(p)
            holder["p"].set_user_dict(ud)
            print(f"user dict reloaded: en={len(ud.en)} word={len(ud.words)} fix={len(ud.fixes)} errors={len(ud.errors)}", flush=True)

    # 先に待ち受けを開く。トレイアプリは /health が返るまで「準備中」と表示する
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(convert, ready))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from core.pipeline import CorePipeline  # 既定は本人の選択（句読点 B・英字は打ったまま。docs/decisions.md D14）
    set_threads(args.threads)
    device = pick_device(args.device)
    t0 = time.perf_counter()
    from core.userdict import default_path, load as load_user
    ud = load_user(default_path())   # 本人のユーザー辞書（docs/REVIEW.md）。中身はログに書かない（件数だけ）
    print(f"user dict: {default_path()} en={len(ud.en)} word={len(ud.words)} fix={len(ud.fixes)} errors={len(ud.errors)}", flush=True)
    holder["p"] = CorePipeline(model=args.model, device=device, user_dict=ud)
    holder["ud_mtime"] = default_path().stat().st_mtime if default_path().exists() else 0.0
    holder["p"].convert_nbest("konnichiha", order=args.window)  # 最初の 1 件の遅さ（CUDA の初期化・辞書の索引）を起動時に済ませる
    ready.set()
    import torch
    print(f"romaji-slm ime server: 127.0.0.1:{args.port} device={device} threads={torch.get_num_threads()} "
          f"ready in {time.perf_counter() - t0:.1f}s", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
