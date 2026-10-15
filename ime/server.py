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
    ap.add_argument("--log", help="標準出力と標準エラーの行き先（pythonw では両方とも None で、進捗表示が落ちる）")
    args = ap.parse_args(argv)
    if args.log:
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
        print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} start", flush=True)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 常駐は起動のたびに Hugging Face へ問い合わせない（取得済みのキャッシュを使う）

    ready = threading.Event()
    holder = {}

    def convert(romaji, ctx):
        return holder["p"].convert_nbest(romaji, ctx)

    # 先に待ち受けを開く。トレイアプリは /health が返るまで「準備中」と表示する
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(convert, ready))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from core.pipeline import CorePipeline  # 既定は本人の選択（句読点 B・英字は打ったまま。docs/decisions.md D14）
    device = pick_device(args.device)
    t0 = time.perf_counter()
    holder["p"] = CorePipeline(model=args.model, device=device)
    holder["p"].convert_nbest("konnichiha")  # 最初の 1 件の遅さ（CUDA の初期化・辞書の索引）を起動時に済ませる
    ready.set()
    print(f"romaji-slm ime server: 127.0.0.1:{args.port} device={device} ready in {time.perf_counter() - t0:.1f}s",
          flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
