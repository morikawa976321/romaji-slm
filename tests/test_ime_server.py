import shutil
import subprocess
import sys
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from core.pipeline import CorePipeline
from ime.server import format_response, make_handler, parse_request

ROOT = Path(__file__).resolve().parent.parent


def test_parse_request_splits_romaji_and_context():
    assert parse_request("kyouha\n先に\r\n食べて".encode()) == ("kyouha", "先に食べて")
    assert parse_request(b" ame \n") == ("ame", "")


def test_format_response_one_candidate_per_line():
    assert format_response(12.4, ["雨", "飴\n"]).decode() == "12\n雨\n飴"


def serve(convert):
    ready = threading.Event()
    ready.set()
    srv = HTTPServer(("127.0.0.1", 0), make_handler(convert, ready))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, body: bytes):
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def test_server_round_trip_and_errors():
    seen = []

    def convert(romaji, ctx):
        seen.append((romaji, ctx))
        if romaji == "boom":
            raise RuntimeError("secret input must not leak")
        return [romaji.upper(), "x"]

    srv, url = serve(convert)
    try:
        lines = post(url + "/convert", "ame\n前の文".encode()).split("\n")
        assert lines[1:] == ["AME", "x"] and float(lines[0]) >= 0
        assert seen == [("ame", "前の文")]
        with pytest.raises(urllib.error.HTTPError) as e:
            post(url + "/convert", b"boom\n")
        assert e.value.code == 500 and b"secret" not in e.value.read()   # 入力や例外の文面を返さない
        with pytest.raises(urllib.error.HTTPError) as e:
            post(url + "/convert", b"\n")
        assert e.value.code == 400
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            assert r.read().decode().startswith("ok ")
    finally:
        srv.shutdown()


def fake_pipeline(cands, rendered):
    p = CorePipeline.__new__(CorePipeline)
    p.en_case = "typed"
    p.candidates = lambda romaji, ctx: cands
    p._render_best = lambda best, romaji, ctx: rendered
    return p


def cand(text, reading, score=0.0):
    return {"text": text, "reading": reading, "score": score, "reading_obj": None, "bounds": ()}


def test_convert_nbest_first_is_rendered_and_end_mark_is_shared():
    p = fake_pipeline([cand("明日は雨か", "アシタハアメカ"), cand("明日は飴か", "アシタハアメカ"),
                       cand("x", "x", float("-inf"))], "明日は雨か。")
    assert p.convert_nbest("ashitahaameka") == ["明日は雨か。", "明日は飴か。", "あしたはあめか。"]


def test_convert_nbest_keeps_first_even_if_it_equals_the_reading():
    p = fake_pipeline([cand("わたし", "ワタシ"), cand("和菓子", "ワカシ")], "わたし")
    assert p.convert_nbest("watashi") == ["わたし", "和菓子"]


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("pwsh"), reason="csc は Windows の .NET Framework 同梱")
def test_composer_state_machine():
    r = subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "ime" / "build.ps1"), "-Test"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
