"""体験版（V0）のローカル比較ページのサーバ。標準ライブラリだけで動く。

    uv run python -m trial.server --open          # 本物の変換コア（zenz と Mozc 辞書が要る）
    python -m trial.server --mock --open          # 偽の変換（torch もモデルも要らない）

127.0.0.1 にだけ bind する。画面・API・記録の形は docs/TRIAL.md。サーバは左文脈などの状態を持たず、
記録は trial/out/ の JSONL に追記する。
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import machine, summary
from .engine import CoreEngine, MockEngine

TRIAL = Path(__file__).resolve().parent
STATIC = TRIAL / "static"
MAX_INPUT = 500
PORT_TRIES = 20
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 書きかけで止まった行は飛ばす
    return out


class TrialApp:
    def __init__(self, engine, configs: dict, out_dir: Path, version: str | None = None,
                 bench_path: Path = TRIAL / "bench_sentences.jsonl"):
        self.engine, self.configs, self.out = engine, configs, Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        if version is None:
            vf = TRIAL / "VERSION"
            version = vf.read_text(encoding="utf-8").strip() if vf.exists() else "dev"
        self.version = version
        self.bench_sentences = read_jsonl(bench_path)
        self.machine = machine.info()
        # 変換と計測は1本ずつ流す。並べると CPU を取り合い、体感の速度を測れない
        self.run_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.ready = False
        self.load_error: str | None = None

    # --- 起動 -----------------------------------------------------------------
    def preload(self) -> None:
        with self.run_lock:
            try:
                self.engine.preload({cid: self._config(cid) for cid in self.configs["configs"]})
            except Exception as e:  # noqa: BLE001  読めなくてもページは開き、変換ごとにエラーを返す
                self.load_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            self.ready = True

    def record_launch(self, t0_ms: float, first: bool, serve_s: float, ready_at: float | None) -> None:
        entry = {"time": now_iso(), "first": first, "serve_s": round(serve_s, 2),
                 "ready_s": None if ready_at is None else round(ready_at - t0_ms / 1000, 2),
                 "error": self.load_error, "version": self.version}
        self._append("launch.jsonl", entry)

    # --- API ------------------------------------------------------------------
    def _config(self, cid: str) -> dict:
        return {"id": cid, **self.configs["configs"][cid]}

    def _append(self, name: str, obj: dict) -> None:
        with self.file_lock, (self.out / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def info(self) -> dict:
        return {"configs": self.configs, "version": self.version, "machine": self.machine,
                "engine": self.engine.describe(self.configs["configs"]), "ready": self.ready,
                "load_error": self.load_error, "bench_sentences": self.bench_sentences}

    def _ids(self, ids) -> list[str]:
        if ids is None:
            return list(self.configs["configs"])
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("config_ids は文字列の配列か null")
        return ids

    def _convert_one(self, text: str, left_context: str, cid: str) -> dict:
        if cid not in self.configs["configs"]:
            return {"config_id": cid, "text": text, "ms": 0.0, "finished": False, "error": f"未知の設定: {cid}"}
        t0 = time.perf_counter()
        try:
            r = self.engine.convert(text, left_context, self._config(cid))
        except Exception as e:  # noqa: BLE001  原入力を返して続ける
            traceback.print_exc()
            return {"config_id": cid, "text": text, "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "finished": False, "error": f"{type(e).__name__}: {e}"}
        return {"config_id": cid, "text": r["text"], "ms": round(r["ms"], 1), "finished": bool(r["finished"]),
                "error": r.get("error")}

    def convert(self, body: dict) -> dict:
        text, ctx = body.get("text"), body.get("left_context") or ""
        if not isinstance(text, str) or not isinstance(ctx, str):
            raise ValueError("text と left_context は文字列")
        if len(text) > MAX_INPUT:
            raise ValueError(f"入力が長すぎる（{MAX_INPUT} 字まで）")
        ids = self._ids(body.get("config_ids"))
        t0 = time.perf_counter()
        with self.run_lock:
            results = [self._convert_one(text, ctx, cid) for cid in ids]
        return {"results": results, "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def record(self, body) -> dict:
        if not isinstance(body, dict):
            raise ValueError("記録は JSON オブジェクト")
        entry = {"time": now_iso(), **body}
        self._append("records.jsonl", entry)
        return {"ok": True, "time": entry["time"]}

    def records(self) -> dict:
        return {"records": read_jsonl(self.out / "records.jsonl")}

    def bench(self, body: dict) -> dict:
        ids = self._ids(body.get("config_ids"))
        sents = [s["romaji"] for s in self.bench_sentences]
        out = []
        t_all = time.perf_counter()
        with self.run_lock:
            for cid in ids:
                if cid not in self.configs["configs"]:
                    out.append({"config_id": cid, "error": f"未知の設定: {cid}"})
                    continue
                for s in sents[:2]:  # 空回し（初回のキャッシュ・メモリ確保を計測から外す）
                    self._convert_one(s, "", cid)
                rs = [self._convert_one(s, "", cid) for s in sents]
                ms = [r["ms"] for r in rs]
                entry = {"config_id": cid, "n": len(rs), "failed": sum(1 for r in rs if r["error"]),
                         "p50": summary.percentile(ms, 50), "p95": summary.percentile(ms, 95),
                         "max": max(ms) if ms else None, "ms": ms}
                self._append("bench.jsonl", {"time": now_iso(), "version": self.version, "engine": self.engine.name,
                                             "machine": self.machine, **entry})
                out.append(entry)
        return {"results": out, "total_s": round(time.perf_counter() - t_all, 2)}

    def summary(self) -> str:
        return summary.render(self.version, self.machine, self.configs, read_jsonl(self.out / "records.jsonl"),
                              read_jsonl(self.out / "bench.jsonl"), read_jsonl(self.out / "launch.jsonl"))


def make_handler(app: TrialApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "romaji-slm-trial"

        def log_message(self, fmt, *args):
            if self.path.startswith("/api/") and not self.path.startswith("/api/info"):
                sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), CONTENT_TYPES[".json"])

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/info":
                return self._json(app.info())
            if path == "/api/records":
                return self._json(app.records())
            if path == "/api/summary":
                return self._send(200, app.summary().encode("utf-8"), "text/plain; charset=utf-8")
            if path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if not f.is_relative_to(STATIC.resolve()) or not f.is_file():
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            self._send(200, f.read_bytes(), CONTENT_TYPES.get(f.suffix, "application/octet-stream"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            routes = {"/api/convert": app.convert, "/api/record": app.record, "/api/bench": app.bench}
            if path not in routes:
                return self._json({"error": "not found"}, 404)
            try:
                body = self._body()
                if path != "/api/record" and not isinstance(body, dict):
                    raise ValueError("JSON オブジェクトを送る")
                return self._json(routes[path](body))
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


class LocalServer(ThreadingHTTPServer):
    # Windows の SO_REUSEADDR は「使用中のポートにも重ねて bind できる」意味になり、空きポート探しが壊れる。
    # Windows では外して SO_EXCLUSIVEADDRUSE を付ける
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind(app: TrialApp, host: str = "127.0.0.1", port: int = 8765, tries: int = PORT_TRIES) -> LocalServer:
    """port から順に空きを探して bind する。port=0 なら OS に選ばせる。"""
    last: OSError | None = None
    for p in ([0] if port == 0 else range(port, port + tries)):
        try:
            return LocalServer((host, p), make_handler(app))
        except OSError as e:
            last = e
    raise OSError(f"{port}〜{port + tries - 1} に空きポートが無い: {last}")


def load_configs(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for a in cfg.get("axes", []):
        missing = [c for c in a.get("configs", []) if c not in cfg["configs"]]
        if missing:
            raise ValueError(f"軸 {a['id']} の設定が configs に無い: {missing}")
    if cfg.get("default_single") not in cfg["configs"]:
        raise ValueError(f"default_single が configs に無い: {cfg.get('default_single')}")
    return cfg


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="romaji-slm 体験版の比較ページ")
    ap.add_argument("--port", type=int, default=8765, help="待ち受けポート（使用中なら次の空きを使う）")
    ap.add_argument("--mock", action="store_true", help="偽の変換エンジンを使う（torch・モデル不要）")
    ap.add_argument("--mock-delay", type=float, default=0.0, help="--mock で、偽の ms × この倍率だけ実際に待つ")
    ap.add_argument("--configs", type=Path, default=TRIAL / "configs.json")
    ap.add_argument("--out", type=Path, default=TRIAL / "out", help="記録の置き場所")
    ap.add_argument("--open", action="store_true", help="既定のブラウザでページを開く")
    a = ap.parse_args(argv)

    # run.cmd が開始時刻（UNIX ミリ秒）と、初回の起動か（.venv が無かった）を渡す
    t0_env = os.environ.get("TRIAL_LAUNCH_T0")
    t0_ms = float(t0_env) if t0_env else None
    first = os.environ.get("TRIAL_LAUNCH_FIRST") == "1"

    engine = MockEngine(delay=a.mock_delay) if a.mock else CoreEngine()
    app = TrialApp(engine, load_configs(a.configs), a.out)
    srv = bind(app, port=a.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    serve_s = time.time() - t0_ms / 1000 if t0_ms else None
    print(f"romaji-slm 体験版 {app.version}（エンジン: {engine.name}）: {url}", flush=True)
    print("このウィンドウを閉じると終了します。", flush=True)

    def warm():
        app.preload()
        if t0_ms:
            app.record_launch(t0_ms, first, serve_s, None if app.load_error else time.time())
        print("モデルの読み込み完了" if not app.load_error else f"モデルの読み込みに失敗: {app.load_error}", flush=True)

    threading.Thread(target=warm, daemon=True).start()
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
