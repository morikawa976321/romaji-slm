"""体験版（V0.2）のステップ式ページのサーバ。標準ライブラリだけで動く。

    uv run python -m trial.server --open          # 本物の変換コア（zenz と Mozc 辞書が要る）
    python -m trial.server --mock --open          # 偽の変換（torch もモデルも要らない）

127.0.0.1 にだけ bind する。画面・API・記録の形は docs/TRIAL.md。起動ごとにセッション id を振り、
記録は trial/out/ の JSONL にセッション id 付きで追記する（書けなければ 500 を返し、ページが再試行する）。
"""

import argparse
import json
import os
import secrets
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
RECORD_KINDS = ("compare", "answer", "feel")
MEASURE_PHASES = ("final", "speed")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class SessionMismatch(Exception):
    """ページが古いセッション（サーバの再起動前）のまま送ってきた。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


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
                 examples: dict | None = None, session: str | None = None):
        self.engine, self.configs, self.out = engine, configs, Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        if version is None:
            vf = TRIAL / "VERSION"
            version = vf.read_text(encoding="utf-8").strip() if vf.exists() else "dev"
        self.version = version
        self.examples = examples if examples is not None else load_examples(TRIAL / "examples.json")
        self.session = session or new_session_id()
        self.started = now_iso()
        self.machine = machine.info()
        # 変換と計測は1本ずつ流す。並べると CPU を取り合い、体感の速度を測れない
        self.run_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.ready = False
        self.load_error: str | None = None

    # --- 設定 -----------------------------------------------------------------
    def resolve(self, cid: str) -> dict:
        """設定 id → エンジンに渡す config。名前付き（configs.json の configs）か、組合せ `<model>/<punct>/<en_case>`。"""
        named = self.configs["configs"].get(cid)
        if named is not None:
            base, punct, en_case = named["base"], named["punct"], named["en_case"]
        else:
            parts = cid.split("/") if isinstance(cid, str) else []
            if len(parts) != 3:
                raise KeyError(cid)
            base, punct, en_case = parts
            combo = self.configs["combo"]
            if punct not in combo["punct"] or en_case not in combo["en_case"]:
                raise KeyError(cid)
        if base not in self.configs["models"]:
            raise KeyError(cid)
        m = {k: v for k, v in self.configs["models"][base].items() if k != "label"}
        return {"id": cid, **m, "punct": punct, "en_case": en_case}

    def preload_ids(self) -> list[str]:
        """起動時に作って空回しする設定: 比較の設定と、既定の組合せ（small と xsmall）。"""
        ids = [c for s in self.configs["steps"].values() for c in s["configs"]]
        default = summary.combo_id(self.configs, {}, self.configs["feel_base"])
        ids += [default, summary.with_base(default, self.configs["speed_base"])]
        return list(dict.fromkeys(ids))

    # --- 起動 -----------------------------------------------------------------
    def preload(self) -> None:
        with self.run_lock:
            try:
                self.engine.preload({cid: self.resolve(cid) for cid in self.preload_ids()})
            except Exception as e:  # noqa: BLE001  読めなくてもページは開き、変換ごとにエラーを返す
                self.load_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            self.ready = True

    def record_launch(self, t0_ms: float, first: bool, serve_s: float, ready_at: float | None) -> None:
        entry = {"time": now_iso(), "session": self.session, "first": first, "serve_s": round(serve_s, 2),
                 "ready_s": None if ready_at is None else round(ready_at - t0_ms / 1000, 2),
                 "error": self.load_error, "version": self.version}
        self._append("launch.jsonl", entry)

    def _append(self, name: str, obj: dict) -> None:
        # 失敗は呼び出し側へ投げる（ページが保存の完了を確かめてから次へ進むため、飲み込まない）
        with self.file_lock, (self.out / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _check_session(self, body: dict) -> None:
        if body.get("session") != self.session:
            raise SessionMismatch(f"セッションが違う（サーバ {self.session}、ページ {body.get('session')}）")

    # --- API ------------------------------------------------------------------
    def info(self) -> dict:
        return {"version": self.version, "session": self.session, "started": self.started, "machine": self.machine,
                "engine": self.engine.describe({c: self.resolve(c) for c in self.preload_ids()}),
                "ready": self.ready, "load_error": self.load_error, "configs": self.configs,
                "examples": self.examples}

    def _convert_one(self, text: str, left_context: str, cid: str) -> dict:
        try:
            config = self.resolve(cid)
        except KeyError:
            return {"config_id": cid, "text": text, "ms": 0.0, "finished": False, "error": f"未知の設定: {cid}"}
        t0 = time.perf_counter()
        try:
            r = self.engine.convert(text, left_context, config)
        except Exception as e:  # noqa: BLE001  原入力を返して続ける
            traceback.print_exc()
            return {"config_id": cid, "text": text, "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "finished": False, "error": f"{type(e).__name__}: {e}"}
        return {"config_id": cid, "text": r["text"], "ms": round(r["ms"], 1), "finished": bool(r["finished"]),
                "error": r.get("error")}

    @staticmethod
    def _text_args(body: dict) -> tuple[str, str]:
        text, ctx = body.get("text"), body.get("left_context") or ""
        if not isinstance(text, str) or not isinstance(ctx, str):
            raise ValueError("text と left_context は文字列")
        if len(text) > MAX_INPUT:
            raise ValueError(f"入力が長すぎる（{MAX_INPUT} 字まで）")
        return text, ctx

    @staticmethod
    def _ids(ids) -> list[str]:
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ValueError("config_ids は文字列の配列")
        return ids

    def convert(self, body: dict) -> dict:
        text, ctx = self._text_args(body)
        ids = self._ids(body.get("config_ids"))
        t0 = time.perf_counter()
        with self.run_lock:
            results = [self._convert_one(text, ctx, cid) for cid in ids]
        return {"results": results, "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def prepare(self, body: dict) -> dict:
        """組合せのパイプラインを先に作って空回しする（体感の1回目を遅くしない）。"""
        ids = self._ids(body.get("config_ids"))
        try:
            configs = {cid: self.resolve(cid) for cid in ids}
        except KeyError as e:
            raise ValueError(f"未知の設定: {e.args[0]}") from None
        with self.run_lock:
            self.engine.preload(configs)
        return {"ok": True}

    def record(self, body: dict) -> dict:
        self._check_session(body)
        if body.get("kind") not in RECORD_KINDS:
            raise ValueError(f"kind は {RECORD_KINDS} のどれか")
        if not isinstance(body.get("id"), str) or not body["id"]:
            raise ValueError("id は空でない文字列")
        entry = {"time": now_iso(), **body}
        self._append("records.jsonl", entry)
        return {"ok": True, "time": entry["time"]}

    def measure(self, body: dict) -> dict:
        """計測の1文。warmup でなければ measure.jsonl に追記する。"""
        self._check_session(body)
        text, _ = self._text_args(body)
        cid, phase, index = body.get("config_id"), body.get("phase"), body.get("index")
        if phase not in MEASURE_PHASES or not isinstance(index, int) or not isinstance(cid, str):
            raise ValueError(f"phase（{MEASURE_PHASES}）・index（整数）・config_id（文字列）が要る")
        warmup = bool(body.get("warmup"))
        with self.run_lock:
            r = self._convert_one(text, "", cid)
        if not warmup:
            self._append("measure.jsonl", {"time": now_iso(), "session": self.session, "version": self.version,
                                           "engine": self.engine.name, "phase": phase, "index": index,
                                           "input": text, **r})
        return {"result": r, "warmup": warmup}

    def _session_rows(self, name: str) -> list[dict]:
        return [r for r in read_jsonl(self.out / name) if r.get("session") == self.session]

    def state(self) -> dict:
        return {"session": self.session, "records": summary.latest_by_id(self._session_rows("records.jsonl")),
                "measures": self._session_rows("measure.jsonl")}

    def summary(self, body: dict | None = None) -> str:
        client = []
        if body:
            self._check_session(body)
            client = body.get("client_records") or []
            if not isinstance(client, list) or not all(isinstance(r, dict) for r in client):
                raise ValueError("client_records はオブジェクトの配列")
        all_records = read_jsonl(self.out / "records.jsonl")
        return summary.render(
            version=self.version, session=self.session, machine=self.machine, engine=self.engine.name,
            configs=self.configs, records=[r for r in all_records if r.get("session") == self.session],
            client_records=client, measures=self._session_rows("measure.jsonl"),
            launch=read_jsonl(self.out / "launch.jsonl"),
            past=[r for r in all_records if r.get("session") != self.session])


def make_handler(app: TrialApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "romaji-slm-trial"

        def log_message(self, fmt, *args):
            if self.path.startswith("/api/") and not self.path.startswith(("/api/info", "/api/state")):
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

        def _text(self, s: str) -> None:
            self._send(200, s.encode("utf-8"), "text/plain; charset=utf-8")

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/info":
                return self._json(app.info())
            if path == "/api/state":
                return self._json(app.state())
            if path == "/api/summary":
                return self._text(app.summary())
            if path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if not f.is_relative_to(STATIC.resolve()) or not f.is_file():
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            self._send(200, f.read_bytes(), CONTENT_TYPES.get(f.suffix, "application/octet-stream"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            routes = {"/api/convert": app.convert, "/api/prepare": app.prepare, "/api/record": app.record,
                      "/api/measure": app.measure, "/api/summary": app.summary}
            if path not in routes:
                return self._json({"error": "not found"}, 404)
            try:
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError("JSON オブジェクトを送る")
                out = routes[path](body)
                return self._text(out) if isinstance(out, str) else self._json(out)
            except SessionMismatch as e:
                return self._json({"error": str(e), "session": app.session}, HTTPStatus.CONFLICT)
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # noqa: BLE001  保存の失敗もここ。ページは 500 を見て再試行する
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
    models, combo, qs = cfg["models"], cfg["combo"], cfg["questions"]
    for cid, c in cfg["configs"].items():
        if c.get("base") not in models:
            raise ValueError(f"設定 {cid} の base が models に無い: {c.get('base')}")
    for sid, s in cfg["steps"].items():
        missing = [c for c in s["configs"] if c not in cfg["configs"]]
        if missing:
            raise ValueError(f"ステップ {sid} の設定が configs に無い: {missing}")
        if s.get("question") not in qs:
            raise ValueError(f"ステップ {sid} の question が questions に無い: {s.get('question')}")
    for key in ("punct", "en_case"):
        for o in qs[key]["options"]:
            if o.get(key) not in combo[key]:
                raise ValueError(f"questions.{key} の選択肢 {o['value']} の {key} が combo に無い: {o.get(key)}")
    for key in ("feel_base", "speed_base"):
        if cfg.get(key) not in models:
            raise ValueError(f"{key} が models に無い: {cfg.get(key)}")
    return cfg


def load_examples(path: Path) -> dict:
    ex = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("punct", "en_case"):
        if not ex.get(key) or not all(isinstance(e.get("romaji"), str) and e["romaji"] for e in ex[key]):
            raise ValueError(f"examples.{key} は romaji を持つ例の配列")
    if len(ex.get("feel", [])) < 2 or not all({"punct", "nopunct"} <= set(e) for e in ex["feel"]):
        raise ValueError("examples.feel は punct と nopunct を持つ例が2つ以上")
    if not ex.get("measure", {}).get("final") or not ex["measure"].get("speed"):
        raise ValueError("examples.measure に final と speed が要る")
    return ex


def announce(app: TrialApp, url: str) -> None:
    """実際の URL を標準出力に1行で出し（run.ps1 が黒いウィンドウに映す）、out/url.txt にも書く。"""
    print(f"romaji-slm 体験版 {app.version}（エンジン: {app.engine.name}、セッション {app.session}）。ページの URL:",
          flush=True)
    print(url, flush=True)
    try:
        (app.out / "url.txt").write_text(url + "\n", encoding="utf-8")
    except OSError as e:
        print(f"url.txt を書けませんでした: {e}", flush=True)
    print("このウィンドウを閉じると終了します。", flush=True)


def open_browser(url: str) -> bool:
    try:
        ok = webbrowser.open(url)
    except Exception as e:  # noqa: BLE001  ブラウザが無い・起動に失敗した環境でもサーバは続ける
        print(f"ブラウザを開けませんでした（{type(e).__name__}: {e}）。", flush=True)
        ok = False
    if not ok:
        print("ブラウザを開けませんでした。上の URL をブラウザのアドレス欄に貼って開いてください。", flush=True)
    return ok


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="romaji-slm 体験版のページ")
    ap.add_argument("--port", type=int, default=8765, help="待ち受けポート（使用中なら次の空きを使う）")
    ap.add_argument("--mock", action="store_true", help="偽の変換エンジンを使う（torch・モデル不要）")
    ap.add_argument("--mock-delay", type=float, default=0.0, help="--mock で、偽の ms × この倍率だけ実際に待つ")
    ap.add_argument("--configs", type=Path, default=TRIAL / "configs.json")
    ap.add_argument("--examples", type=Path, default=TRIAL / "examples.json")
    ap.add_argument("--out", type=Path, default=TRIAL / "out", help="記録の置き場所")
    ap.add_argument("--open", action="store_true", help="既定のブラウザでページを開く")
    a = ap.parse_args(argv)

    # run.ps1 が開始時刻（UNIX ミリ秒）と、初回の起動か（.venv が無かった）を渡す
    t0_env = os.environ.get("TRIAL_LAUNCH_T0")
    t0_ms = float(t0_env) if t0_env else None
    first = os.environ.get("TRIAL_LAUNCH_FIRST") == "1"

    engine = MockEngine(delay=a.mock_delay) if a.mock else CoreEngine()
    app = TrialApp(engine, load_configs(a.configs), a.out, examples=load_examples(a.examples))
    srv = bind(app, port=a.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    serve_s = time.time() - t0_ms / 1000 if t0_ms else None
    announce(app, url)

    def warm():
        app.preload()
        if t0_ms:
            try:
                app.record_launch(t0_ms, first, serve_s, None if app.load_error else time.time())
            except OSError:
                traceback.print_exc()
        print("準備できました" if not app.load_error else f"モデルの読み込みに失敗: {app.load_error}", flush=True)

    threading.Thread(target=warm, daemon=True).start()
    if a.open:
        open_browser(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
