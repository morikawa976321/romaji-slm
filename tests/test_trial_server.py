import json
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from eval.converters import Result
from trial import summary
from trial.engine import UNFINISHED, CoreEngine, MockEngine
from trial.server import TrialApp, bind, load_configs

CONFIGS = Path(__file__).resolve().parent.parent / "trial" / "configs.json"


@contextmanager
def running(engine, out_dir):
    app = TrialApp(engine, load_configs(CONFIGS), out_dir, version="test")
    app.preload()
    srv = bind(app, port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield app, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        body = r.read().decode("utf-8")
        return json.loads(body) if "json" in r.headers["Content-Type"] else body


def post(url, obj):
    req = urllib.request.Request(url, json.dumps(obj).encode("utf-8"), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture
def server(tmp_path):
    with running(MockEngine(), tmp_path) as (app, base):
        yield app, base, tmp_path


def test_info(server):
    _, base, _ = server
    info = get(base + "/api/info")
    assert info["version"] == "test"
    assert info["ready"] and info["load_error"] is None
    assert info["configs"]["default_single"] in info["configs"]["configs"]
    assert {"cpu", "logical_cores", "memory_gb", "os", "python", "torch"} <= set(info["machine"])
    assert len(info["bench_sentences"]) == 20
    assert get(base + "/").startswith("<!doctype html>")


def test_convert_returns_each_config_in_order(server):
    _, base, _ = server
    r = post(base + "/api/convert", {"text": "kyouhaiitenki", "left_context": "", "config_ids": ["fast", "accurate"]})
    assert [x["config_id"] for x in r["results"]] == ["fast", "accurate"]
    assert all(x["finished"] and x["error"] is None for x in r["results"])
    assert r["results"][0]["text"] != r["results"][1]["text"]  # MockEngine は設定ごとに違う規則
    assert r["total_ms"] >= 0


def test_convert_error_returns_original_input(server):
    _, base, _ = server
    r = post(base + "/api/convert", {"text": "abc!!err", "left_context": "", "config_ids": ["fast", "nope"]})
    assert [x["text"] for x in r["results"]] == ["abc!!err", "abc!!err"]
    assert all(x["error"] and not x["finished"] for x in r["results"])
    r = post(base + "/api/convert", {"text": "abc!!unfinished", "left_context": "", "config_ids": ["fast"]})
    assert r["results"][0]["text"] == "abc!!unfinished" and r["results"][0]["error"] == UNFINISHED


def test_convert_null_config_ids_runs_all(server):
    _, base, _ = server
    r = post(base + "/api/convert", {"text": "a", "left_context": "", "config_ids": None})
    assert len(r["results"]) == len(load_configs(CONFIGS)["configs"])


def test_convert_bad_request(server):
    _, base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/convert", {"text": 1})
    assert e.value.code == 400


class SlowEngine(MockEngine):
    """同時に何本走っているかを数える。"""

    def __init__(self):
        super().__init__()
        self.active = self.peak = 0
        self.calls = []
        self.lock = threading.Lock()

    def convert(self, text, left_context, config):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((config["id"], text, left_context))
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return super().convert(text, left_context, config)


def test_convert_runs_sequentially_across_requests(tmp_path):
    eng = SlowEngine()
    with running(eng, tmp_path) as (_, base):
        ths = [threading.Thread(target=post, args=(base + "/api/convert",
                                                   {"text": f"t{i}", "left_context": "ctx", "config_ids": ["fast", "accurate"]}))
               for i in range(3)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
    assert eng.peak == 1
    assert len(eng.calls) == 6 and all(c[2] == "ctx" for c in eng.calls)


def test_record_appends_and_lists(server):
    _, base, out = server
    post(base + "/api/record", {"kind": "single", "id": "s1", "input": "a", "verdict": None})
    post(base + "/api/record", {"kind": "single", "id": "s1", "input": "a", "verdict": "used"})
    recs = get(base + "/api/records")["records"]
    assert [r["verdict"] for r in recs] == [None, "used"]
    assert all("time" in r for r in recs)
    lines = (out / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_bench_warms_up_and_writes(tmp_path):
    eng = SlowEngine()
    with running(eng, tmp_path) as (_, base):
        r = post(base + "/api/bench", {"config_ids": ["fast"]})
    (b,) = r["results"]
    assert b["config_id"] == "fast" and b["n"] == 20 and b["failed"] == 0
    assert b["p50"] <= b["p95"] <= b["max"]
    assert len(eng.calls) == 22  # 空回し 2 回 + 20 文
    saved = [json.loads(x) for x in (tmp_path / "bench.jsonl").read_text(encoding="utf-8").splitlines()]
    assert saved[0]["config_id"] == "fast" and saved[0]["p95"] == b["p95"]


def test_summary_round_trip(server):
    app, base, out = server
    cards = [{"label": "A", "config_id": "accurate", "text": "あ", "ms": 30, "error": None},
             {"label": "B", "config_id": "fast", "text": "ア", "ms": 10, "error": None}]
    base_rec = {"kind": "compare", "id": "c1", "axis": "speed", "input": "a", "left_context": "", "cards": cards}
    post(base + "/api/record", {**base_rec, "best": "A", "fatal": [], "all_ok": False})
    post(base + "/api/record", {**base_rec, "best": "B", "fatal": ["A"], "all_ok": False})  # 選び直し
    post(base + "/api/record", {**base_rec, "id": "c2", "best": None, "fatal": [], "all_ok": True})
    post(base + "/api/record", {"kind": "single", "id": "s1", "config_id": "accurate", "input": "b",
                                "left_context": "", "text": "び", "ms": 50, "error": None, "verdict": "fixed"})
    post(base + "/api/bench", {"config_ids": ["accurate"]})
    (out / "launch.jsonl").write_text(json.dumps({"first": True, "serve_s": 12.3, "ready_s": 40.5}) + "\n",
                                      encoding="utf-8")
    s = get(base + "/api/summary")
    lines = s.splitlines()
    assert lines[0].startswith("romaji-slm trial test / ")
    assert lines[1].startswith("機種: ")
    assert lines[2] == "初回起動: 応答 12.3秒・モデル読込完了 40.5秒"
    assert lines[3].startswith("常駐計測: accurate p50=")
    assert "[速度/精度] 入力 2 件: 一番 fast=1 accurate=0; 致命的 fast=0 accurate=1; 全部問題ない 1" in s
    assert "[打ってみる] 1 件, 使えた 0, 直した 1, 中央値 50ms" in s
    assert '1. [速度/精度] "a" | A(accurate)=あ 30ms | B(fast)=ア 10ms | 一番=B 致命的=A' in s
    assert '2. [速度/精度] "a" | A(accurate)=あ 30ms | B(fast)=ア 10ms | 全部問題ない' in s
    assert '3. [打ってみる accurate] "b" → び 50ms | 直した' in s


def test_percentile():
    assert summary.percentile([], 50) is None
    assert summary.percentile([3, 1, 2], 50) == 2
    assert summary.percentile(list(range(1, 21)), 95) == 19
    assert summary.percentile([5], 95) == 5


def test_bind_skips_used_port(tmp_path):
    app = TrialApp(MockEngine(), load_configs(CONFIGS), tmp_path, version="t")
    a = bind(app, port=0)
    try:
        b = bind(app, port=a.server_address[1], tries=5)
        try:
            assert b.server_address[1] != a.server_address[1]
        finally:
            b.server_close()
    finally:
        a.server_close()


# --- CoreEngine: CorePipeline を偽物に差し替えて、torch なしで確かめる ------------------------


def fake_core(accepts_new: bool):
    """core.pipeline と同じ形（モジュールの ZenzScorer・Dictionary を __init__ で使う）の偽物。"""
    mod = types.ModuleType(f"fake_pipeline_{accepts_new}")
    mod.loads = {"scorer": [], "dic": 0}

    class ZenzScorer:
        def __init__(self, model, device):
            mod.loads["scorer"].append((model, device))

    class Dictionary:
        @staticmethod
        def load():
            mod.loads["dic"] += 1
            return object()

    class Base:
        def _init(self, model, device, k, w_read, w_flu, **extra):
            self.dic = mod.Dictionary.load()
            self.scorer = mod.ZenzScorer(model, device)
            self.args = dict(model=model, device=device, k=k, w_read=w_read, w_flu=w_flu, **extra)

        def candidates(self, romaji, left_context=""):
            score = float("-inf") if "x" in romaji else -1.0
            return [{"text": romaji.upper(), "score": score, "edits": []}]

        def convert_detailed(self, romaji, left_context=""):
            cands = self.candidates(romaji, left_context)
            return Result(cands[0]["text"], candidates=tuple(c["text"] for c in cands))

    if accepts_new:
        class CorePipeline(Base):
            def __init__(self, model="m", device="cpu", k=5, n_read=20, w_read=4.0, w_flu=1.0, punct="auto",
                         en_case="registered", name=None):
                self._init(model, device, k, w_read, w_flu, punct=punct, en_case=en_case)
    else:
        class CorePipeline(Base):
            def __init__(self, model="m", device="cpu", k=5, n_read=20, w_read=4.0, w_flu=1.0, name=None):
                self._init(model, device, k, w_read, w_flu)

    for c in (ZenzScorer, Dictionary, CorePipeline):
        c.__module__ = mod.__name__
    mod.ZenzScorer, mod.Dictionary, mod.CorePipeline = ZenzScorer, Dictionary, CorePipeline
    sys.modules[mod.__name__] = mod
    return mod


CFG = {"model": "small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "insert", "en_case": "auto"}


def test_core_engine_maps_only_accepted_args():
    mod = fake_core(accepts_new=False)
    eng = CoreEngine(mod.CorePipeline)
    kwargs, ignored = eng.pipeline_kwargs(CFG)
    assert kwargs == {"model": "small", "device": "cpu", "k": 5, "w_read": 4.0, "w_flu": 1.0}
    assert ignored == ["punct", "en_case"]
    assert eng.describe({"a": CFG})["ignored"] == {"a": ["punct", "en_case"]}

    mod2 = fake_core(accepts_new=True)
    eng2 = CoreEngine(mod2.CorePipeline)
    kwargs, ignored = eng2.pipeline_kwargs(CFG)
    assert kwargs["punct"] == "insert" and kwargs["en_case"] == "auto" and ignored == []
    eng2.convert("ab", "", CFG)
    (pipe,) = eng2._pipes.values()
    assert pipe.args == {"model": "small", "device": "cpu", "k": 5, "w_read": 4.0, "w_flu": 1.0,
                         "punct": "insert", "en_case": "auto"}


def test_core_engine_shares_model_per_model_and_device():
    mod = fake_core(accepts_new=True)
    eng = CoreEngine(mod.CorePipeline)
    eng.preload({"a": CFG, "b": {**CFG, "k": 3}, "c": {**CFG, "punct": "ja"}, "d": {**CFG, "model": "xsmall"},
                 "a2": dict(CFG)})
    assert mod.loads["scorer"] == [("small", "cpu"), ("xsmall", "cpu")]
    assert mod.loads["dic"] == 2
    assert len(eng._pipes) == 4  # a と a2 は同じ引数なので同じパイプライン
    # 差し替えはその場限り
    assert mod.ZenzScorer.__name__ == "ZenzScorer" and hasattr(mod.Dictionary, "load")
    pipes = list(eng._pipes.values())
    assert pipes[1].scorer is pipes[0].scorer and pipes[1].args["k"] == 3


def test_core_engine_finished_and_failure():
    mod = fake_core(accepts_new=False)
    eng = CoreEngine(mod.CorePipeline)
    ok = eng.convert("ab", "ctx", CFG)
    assert ok["finished"] and ok["text"] == "AB" and ok["error"] is None and ok["candidates"] == ["AB"]
    bad = eng.convert("xy", "", CFG)  # 最良の score が -inf
    assert not bad["finished"] and bad["text"] == "xy" and bad["error"] == UNFINISHED
    (pipe,) = eng._pipes.values()
    assert "candidates" not in pipe.__dict__  # 横取りの差し込みを外している
