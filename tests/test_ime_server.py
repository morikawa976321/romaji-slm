import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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


def serve(convert, idle=None):
    ready = threading.Event()
    ready.set()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(convert, ready, idle))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, body: bytes):
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def test_server_round_trip_and_errors():
    seen = []

    def convert(romaji, ctx, full):
        seen.append((romaji, ctx, full))
        if romaji == "boom":
            raise RuntimeError("secret input must not leak")
        return [romaji.upper(), "x"]

    srv, url = serve(convert)
    try:
        lines = post(url + "/convert", "ame\n前の文".encode()).split("\n")
        assert lines[1:] == ["AME", "x"] and float(lines[0]) >= 0
        assert seen == [("ame", "前の文", True)]   # mode を付けなければ、別の書き方まで揃った列
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


def test_idle_connection_does_not_block_other_requests():
    import socket
    srv, url = serve(lambda r, c, f: [r])
    idle = socket.create_connection(srv.server_address)   # 何も送らずに保持する
    try:
        assert post(url + "/convert", b"ame\n").split("\n")[1] == "ame"
    finally:
        idle.close()
        srv.shutdown()


def post_status(url, body: bytes):
    try:
        return 200, post(url, body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def slow(started: threading.Event, done: list, steps: int = 150):
    """forward の切れ目ごとに core.cancel.check を呼ぶ、遅い変換の代わり。"""
    from core.cancel import check

    def convert(romaji, ctx, full):
        started.set()
        for _ in range(steps):
            check()
            time.sleep(0.01)
        done.append(romaji)
        return [romaji]
    return convert


def test_first_mode_answers_then_prepares_full_while_idle():
    calls, prepared = [], threading.Event()

    def convert(romaji, ctx, full):
        calls.append((romaji, full))
        return [romaji + ("+alt" if full else "")]

    def idle(romaji, ctx):
        calls.append((romaji, "idle"))
        prepared.set()

    srv, url = serve(convert, idle)
    try:
        assert post(url + "/convert?mode=first&prefetch=1", b"ame\n").split("\n")[1:] == ["ame"]
        assert prepared.wait(5)
        assert post(url + "/convert?mode=full&prefetch=1", b"ame\n").split("\n")[1:] == ["ame+alt"]
        assert calls == [("ame", False), ("ame", "idle"), ("ame", True)]
    finally:
        srv.shutdown()


def test_newer_request_supersedes_prefetch_but_not_commit():
    started, done = threading.Event(), []
    srv, url = serve(slow(started, done))
    try:
        # 先読み（prefetch=1）は、後から別の要求が来たら forward の切れ目でやめて 409
        res = {}
        t = threading.Thread(target=lambda: res.update(a=post_status(url + "/convert?mode=first&prefetch=1", b"kyou\n")))
        t.start()
        assert started.wait(5)
        t0 = time.perf_counter()
        assert post_status(url + "/convert?mode=first&prefetch=1", b"kyouha\n")[0] == 200
        t.join(10)
        assert res["a"][0] == 409 and done == ["kyouha"]
        assert time.perf_counter() - t0 < 2.6   # 古い方を最後まで待っていない（1 つ 1.5 秒かかる）
        # 確定の要求（prefetch 無し）は、後から要求が来てもやめない
        started.clear()
        t = threading.Thread(target=lambda: res.update(c=post_status(url + "/convert?mode=first", b"ame\n")))
        t.start()
        assert started.wait(5)
        assert post_status(url + "/convert?mode=first&prefetch=1", b"amed\n")[0] == 200
        t.join(10)
        assert res["c"][0] == 200 and done[-2:] == ["ame", "amed"]
    finally:
        srv.shutdown()


def test_idle_preparation_stops_for_another_request_but_not_for_its_own_full():
    started, finished, stopped = threading.Event(), [], []
    from core.cancel import Cancelled, check

    def idle(romaji, ctx):
        started.set()
        try:
            for _ in range(100):
                check()
                time.sleep(0.01)
        except Cancelled:
            stopped.append(romaji)
            raise
        finished.append(romaji)

    srv, url = serve(lambda r, c, f: [r + ("+alt" if f else "")], idle)
    try:
        post(url + "/convert?mode=first&prefetch=1", b"a\n")
        assert started.wait(5)
        assert post(url + "/convert?mode=first&prefetch=1", b"ab\n").split("\n")[1] == "ab"   # 別の打鍵列: 用意をやめる
        started.clear()
        assert started.wait(5)   # ab の用意が始まる
        assert post(url + "/convert?mode=full&prefetch=1", b"ab\n").split("\n")[1] == "ab+alt"   # 同じ打鍵列の full: 続ける
        assert stopped == ["a"] and finished == ["ab"]
    finally:
        srv.shutdown()


def fake_pipeline(cands, rendered):
    p = CorePipeline.__new__(CorePipeline)
    p.en_case = "typed"
    p.candidates = lambda romaji, ctx: cands
    p._render_best = lambda best, romaji, ctx: rendered
    return p


def cand(text, reading, score=0.0):
    return {"text": text, "reading": reading, "score": score, "reading_obj": None, "bounds": (), "edits": []}


def test_convert_nbest_first_is_rendered_and_end_mark_is_shared():
    p = fake_pipeline([cand("明日は雨か", "アシタハアメカ"), cand("明日は飴か", "アシタハアメカ"),
                       cand("x", "x", float("-inf"))], "明日は雨か。")
    assert p.convert_nbest("ashitahaameka", order="reading") == ["明日は雨か。", "明日は飴か。", "あしたはあめか。"]


def test_convert_nbest_keeps_first_even_if_it_equals_the_reading():
    p = fake_pipeline([cand("わたし", "ワタシ"), cand("和菓子", "ワカシ")], "わたし")
    assert p.convert_nbest("watashi", order="reading") == ["わたし", "和菓子"]


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("pwsh"), reason="csc は Windows の .NET Framework 同梱")
def test_composer_state_machine():
    r = subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "ime" / "build.ps1"), "-Test"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
