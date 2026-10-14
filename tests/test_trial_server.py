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
from trial import server as trial_server
from trial import summary
from trial.engine import UNFINISHED, CoreEngine, MockEngine
from trial.server import TrialApp, bind, load_configs, load_examples

TRIAL = Path(__file__).resolve().parent.parent / "trial"
CONFIGS = TRIAL / "configs.json"
EXAMPLES = TRIAL / "examples.json"


@contextmanager
def running(engine, out_dir, session=None):
    app = TrialApp(engine, load_configs(CONFIGS), out_dir, version="test", examples=load_examples(EXAMPLES),
                   session=session)
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
        body = r.read().decode("utf-8")
        return json.loads(body) if "json" in r.headers["Content-Type"] else body


def post_status(url, obj) -> int:
    try:
        post(url, obj)
    except urllib.error.HTTPError as e:
        return e.code
    return 200


@pytest.fixture
def server(tmp_path):
    with running(MockEngine(), tmp_path, session="S1") as (app, base):
        yield app, base, tmp_path


def rec(**kw):
    return {"session": "S1", "rev": 1, **kw}


# --- 設定と例文 ----------------------------------------------------------------


def test_configs_and_examples_are_consistent():
    cfg, ex = load_configs(CONFIGS), load_examples(EXAMPLES)
    assert cfg["steps"]["punct"]["configs"] == ["punct_a", "punct_b"]
    assert {cfg["configs"][c]["punct"] for c in ("punct_a", "punct_b")} == {"ja", "insert"}
    assert all(cfg["configs"][c]["en_case"] == "typed" for c in ("punct_a", "punct_b"))
    assert [cfg["configs"][c]["en_case"] for c in cfg["steps"]["en_case"]["configs"]] == ["typed", "registered", "auto"]
    assert all(cfg["models"][m]["k"] == 5 for m in cfg["models"])
    # 句読点の比較は句読点を打たない入力でないと B の挿入が働かない
    assert all(not set(",.?!") & set(e["romaji"]) for e in ex["punct"])
    assert all(not set(",.?!") & set(e["nopunct"]) for e in ex["feel"])
    assert len(ex["measure"]["final"]) == 6 and len(ex["measure"]["speed"]) == 3
    assert sum(1 for t in ex["measure"]["final"] if not set(",.?!") & set(t)) == 2


def test_load_configs_rejects_unknown_refs(tmp_path):
    cfg = json.loads(CONFIGS.read_text(encoding="utf-8"))
    cfg["steps"]["punct"]["configs"].append("nope")
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="nope"):
        load_configs(p)


def test_resolve_named_and_combo(tmp_path):
    app = TrialApp(MockEngine(), load_configs(CONFIGS), tmp_path, version="t", examples=load_examples(EXAMPLES))
    a = app.resolve("punct_b")
    assert a["model"].endswith("zenz-v3.1-small") and a["punct"] == "insert" and a["en_case"] == "typed"
    assert "label" not in a and a["id"] == "punct_b"
    c = app.resolve("xsmall/ja/registered")
    assert c["model"].endswith("zenz-v3.1-xsmall") and c["k"] == 5 and c["punct"] == "ja" and c["en_case"] == "registered"
    for bad in ("small/ascii/typed", "huge/ja/typed", "small/ja", "accurate"):
        with pytest.raises(KeyError):
            app.resolve(bad)
    assert app.preload_ids()[-2:] == ["small/ja/typed", "xsmall/ja/typed"]


# --- API ------------------------------------------------------------------------


def test_info(server):
    app, base, _ = server
    info = get(base + "/api/info")
    assert info["version"] == "test" and info["session"] == "S1"
    assert info["ready"] and info["load_error"] is None
    assert {"cpu", "logical_cores", "memory_gb", "os", "python", "torch"} <= set(info["machine"])
    assert len(info["examples"]["punct"]) == 3 and "questions" in info["configs"]
    assert set(info["engine"]["ignored"]) == set(app.preload_ids())
    assert app.engine.preloaded == app.preload_ids()
    assert get(base + "/").startswith("<!doctype html>")


def test_convert_returns_each_config_in_order(server):
    _, base, _ = server
    r = post(base + "/api/convert", {"text": "kyouhaiitenki", "left_context": "", "config_ids": ["punct_b", "punct_a"]})
    assert [x["config_id"] for x in r["results"]] == ["punct_b", "punct_a"]
    assert all(x["finished"] and x["error"] is None for x in r["results"])
    assert r["results"][0]["text"].endswith("。") and "、" in r["results"][0]["text"]
    assert not set("。、") & set(r["results"][1]["text"])  # A は打たない句読点を足さない
    assert r["total_ms"] >= 0


def test_convert_en_case_differs(server):
    _, base, _ = server
    ids = ["en_typed", "en_registered", "en_auto"]
    outs = [x["text"] for x in post(base + "/api/convert", {"text": "githubtojrnoeki.", "left_context": "",
                                                             "config_ids": ids})["results"]]
    assert outs[0].startswith("github") and outs[1].startswith("GitHub") and "JR" in outs[2] and "jr" in outs[1]


def test_convert_error_returns_original_input(server):
    _, base, _ = server
    r = post(base + "/api/convert", {"text": "abc!!err", "left_context": "", "config_ids": ["punct_a", "nope"]})
    assert [x["text"] for x in r["results"]] == ["abc!!err", "abc!!err"]
    assert all(x["error"] and not x["finished"] for x in r["results"])
    r = post(base + "/api/convert", {"text": "abc!!unfinished", "left_context": "", "config_ids": ["punct_a"]})
    assert r["results"][0]["text"] == "abc!!unfinished" and r["results"][0]["error"] == UNFINISHED


def test_convert_bad_request(server):
    _, base, _ = server
    assert post_status(base + "/api/convert", {"text": 1, "config_ids": ["punct_a"]}) == 400
    assert post_status(base + "/api/convert", {"text": "a", "config_ids": None}) == 400
    assert post_status(base + "/api/nope", {}) == 404


def test_prepare_warms_combos(server):
    app, base, _ = server
    assert post(base + "/api/prepare", {"config_ids": ["small/insert/auto", "xsmall/insert/auto"]})["ok"]
    assert app.engine.preloaded[-2:] == ["small/insert/auto", "xsmall/insert/auto"]
    assert post_status(base + "/api/prepare", {"config_ids": ["small/x/auto"]}) == 400


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
    with running(eng, tmp_path, session="S1") as (_, base):
        ths = [threading.Thread(target=post, args=(base + "/api/convert",
                                                   {"text": f"t{i}", "left_context": "ctx",
                                                    "config_ids": ["punct_a", "punct_b"]}))
               for i in range(3)]
        ths.append(threading.Thread(target=post, args=(base + "/api/measure", {
            "session": "S1", "phase": "final", "config_id": "small/ja/typed", "index": 0, "text": "m"})))
        for t in ths:
            t.start()
        for t in ths:
            t.join()
    assert eng.peak == 1
    assert len(eng.calls) == 7 and sum(c[2] == "ctx" for c in eng.calls) == 6


# --- 記録: 保存の完了・失敗・セッション ----------------------------------------------


def test_record_is_on_disk_when_response_returns(server):
    _, base, out = server
    r = post(base + "/api/record", rec(kind="answer", id="answer-punct", question="punct", value="B"))
    assert r["ok"] and r["time"]
    (line,) = (out / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["value"] == "B" and json.loads(line)["session"] == "S1"


def test_record_rejects_bad_body_and_old_session(server):
    _, base, out = server
    assert post_status(base + "/api/record", rec(kind="nope", id="x")) == 400
    assert post_status(base + "/api/record", rec(kind="answer", id="")) == 400
    assert post_status(base + "/api/record", {**rec(kind="answer", id="a"), "session": "OLD"}) == 409
    assert not (out / "records.jsonl").exists()


def test_record_failure_is_reported(tmp_path):
    with running(MockEngine(), tmp_path, session="S1") as (_, base):
        (tmp_path / "records.jsonl").mkdir()  # 書けない状態を作る
        assert post_status(base + "/api/record", rec(kind="answer", id="a", question="use", value="want")) == 500


def test_state_and_summary_only_this_session(tmp_path):
    with running(MockEngine(), tmp_path, session="OLD") as (_, base):
        post(base + "/api/record", {**rec(kind="answer", id="answer-punct", question="punct", value="B"), "session": "OLD"})
        post(base + "/api/measure", {"session": "OLD", "phase": "final", "config_id": "small/insert/typed",
                                     "index": 0, "text": "a"})
    with running(MockEngine(), tmp_path, session="S1") as (_, base):
        st = get(base + "/api/state")
        assert st["session"] == "S1" and st["records"] == [] and st["measures"] == []
        post(base + "/api/record", rec(kind="answer", id="answer-use", question="use", value="want"))
        post(base + "/api/record", {**rec(kind="answer", id="answer-use", question="use", value="not_now"), "rev": 2})
        st = get(base + "/api/state")
        assert [r["value"] for r in st["records"]] == ["not_now"]
        s = get(base + "/api/summary")
    assert "【回答】句読点: 未回答（暫定 A）" in s  # 前のセッションの B は混ざらない
    assert "【回答】使用感: 今は使いたくない" in s
    assert "過去の記録: 1 セッション・1 行（今回の集計には含めない）" in s
    assert "small + punct=ja + en_case=typed: 未実行" in s


# --- 計測 ------------------------------------------------------------------------


def test_measure_records_only_non_warmup(server):
    _, base, out = server
    body = {"session": "S1", "phase": "final", "config_id": "small/insert/typed", "index": 0, "text": "kyouha"}
    r = post(base + "/api/measure", {**body, "warmup": True})
    assert r["warmup"] and r["result"]["finished"]
    assert not (out / "measure.jsonl").exists()
    r = post(base + "/api/measure", body)
    assert set(r["result"]) == {"config_id", "text", "ms", "finished", "error"}
    (m,) = get(base + "/api/state")["measures"]
    assert m["phase"] == "final" and m["index"] == 0 and m["input"] == "kyouha" and m["ms"] == r["result"]["ms"]
    assert post_status(base + "/api/measure", {**body, "phase": "p95"}) == 400
    assert post_status(base + "/api/measure", {**body, "session": "OLD"}) == 409


# --- 結果ブロック ---------------------------------------------------------------------


def cards(*pairs):
    return [{"label": f"案{i + 1}", "config_id": c, "text": t, "ms": ms, "error": None} for i, (c, t, ms) in enumerate(pairs)]


def test_summary_round_trip(server):
    app, base, out = server
    (out / "launch.jsonl").write_text(
        json.dumps({"first": True, "serve_s": 12.3, "ready_s": 40.5, "session": "X"}) + "\n"
        + json.dumps({"first": False, "serve_s": 3.0, "ready_s": 15.25, "session": "S1"}) + "\n", encoding="utf-8")
    p1 = rec(kind="compare", id="cmp-punct-0", step="punct", index=0, total=3, input="kyouha",
             cards=cards(("punct_b", "今日は、雨。", 900), ("punct_a", "今日は雨", 800)), choice=None, reason=None)
    post(base + "/api/record", p1)
    post(base + "/api/record", {**p1, "rev": 2, "choice": "punct_b"})
    p2 = rec(kind="compare", id="cmp-punct-1", step="punct", index=1, total=3, input="nanji",
             cards=cards(("punct_a", "何時", 700), ("punct_b", "何時", 600)), choice="neither", reason="content")
    post(base + "/api/record", p2)
    e1 = rec(kind="compare", id="cmp-en_case-0", step="en_case", index=0, total=3, input="github",
             cards=cards(("en_auto", "GitHub", 1), ("en_typed", "github", 2), ("en_registered", "GitHub", 3)),
             choice="all_ok", reason=None)
    post(base + "/api/record", e1)
    post(base + "/api/record", rec(kind="answer", id="answer-punct", question="punct", value="both_ok"))
    post(base + "/api/record", rec(kind="answer", id="answer-en_case", question="en_case", value="auto"))
    post(base + "/api/record", rec(kind="feel", id="feel-1", seq=1, source="example1", config_id="small/ja/auto",
                                   input="ashita", left_context="", text="明日", ms=812.4, error=None))
    post(base + "/api/record", rec(kind="feel", id="feel-2", seq=2, source="speed", config_id="xsmall/ja/auto",
                                   input="ashita", left_context="", text="明日", ms=201, error=None))
    for i, t in enumerate(app.examples["measure"]["final"]):
        post(base + "/api/measure", {"session": "S1", "phase": "final", "config_id": "small/ja/auto", "index": i, "text": t})
    for cid in ("small/ja/auto", "xsmall/ja/auto"):
        for i, t in enumerate(app.examples["measure"]["speed"]):
            post(base + "/api/measure", {"session": "S1", "phase": "speed", "config_id": cid, "index": i, "text": t})
    # 「使用感」の回答はサーバに届かず、ブラウザにだけ残っている
    client = [rec(kind="answer", id="answer-use", question="use", value="want"),
              rec(kind="answer", id="answer-punct", question="punct", value="both_ok")]
    s = post(base + "/api/summary", {"session": "S1", "client_records": client})
    lines = s.splitlines()
    assert lines[0].startswith("romaji-slm trial test / ") and lines[0].endswith(" / session S1")
    assert lines[1].startswith("機種: ") and lines[1].endswith("エンジン mock")
    assert lines[2] == "初回起動: serve 12.3s / ready 40.5s ／ 今回の起動: serve 3.0s / ready 15.2s"
    assert lines[3] == "【回答】句読点: どちらも問題ない（暫定 A）"
    assert lines[4] == "【回答】英字: 自動"
    assert lines[5] == "【回答】使用感: 使ってみたい"
    assert lines[6] == "体感した設定: small + punct=ja + en_case=auto（xsmall も試した: はい）"
    assert lines[7].startswith("計測: small + punct=ja + en_case=auto 6文: 各 ")
    final = [json.loads(x) for x in (out / "measure.jsonl").read_text(encoding="utf-8").splitlines()][:6]
    over = sum(1 for m in final if m["ms"] > 1000)
    assert f"最大 {round(max(m['ms'] for m in final))}ms, 1秒超 {over}件" in lines[7]
    assert " ／ small vs xsmall 3文: small " in lines[7] and " / xsmall " in lines[7]
    assert "p95" not in s
    assert "未保存の記録: 1 件" in s
    assert ('句読点 1/3 "kyouha" A(punct_a)=今日は雨 800ms | B(punct_b)=今日は、雨。 900ms | 差あり | '
            'B: 句点/読点 | 回答: B') in lines
    assert '句読点 2/3 "nanji" A(punct_a)=何時 700ms | B(punct_b)=何時 600ms | 同一出力 | B: 挿入なし | 回答: どちらも使えない(変換内容)' in lines
    assert ('英字 1/3 "github" 打ったまま(en_typed)=github 2ms | 登録語だけ(en_registered)=GitHub 3ms | '
            '自動(en_auto)=GitHub 1ms | 差あり | 回答: どれも問題ない') in lines
    i = lines.index("--- 体感の記録 ---")
    assert lines[i + 1] == '1. [small + punct=ja + en_case=auto] "ashita" → 明日 812ms 文脈=0字（例1）'
    assert lines[i + 2].startswith("2. [xsmall + punct=ja + en_case=auto]")
    assert lines.index("--- 比較の記録 ---") < i


def test_summary_without_anything(server):
    _, base, _ = server
    s = get(base + "/api/summary")
    assert "初回起動: 不明（run.cmd から起動していない）" in s
    assert "【回答】英字: 未回答（暫定 打ったまま）" in s and "【回答】使用感: 未回答" in s
    assert "体感した設定: なし（xsmall も試した: いいえ）" in s
    assert s.rstrip().endswith("--- 体感の記録 ---\nなし")


def test_combo_id():
    cfg = load_configs(CONFIGS)
    assert summary.combo_id(cfg, {}, "small") == "small/ja/typed"
    assert summary.combo_id(cfg, {"punct": "B", "en_case": "registered"}, "small") == "small/insert/registered"
    assert summary.combo_id(cfg, {"punct": "both_ok", "en_case": "all_ok"}, "small") == "small/ja/typed"
    assert summary.with_base("small/insert/auto", "xsmall") == "xsmall/insert/auto"


def test_merge_client_prefers_newer_rev():
    server = [{"id": "a", "rev": 2, "v": "s"}, {"id": "b", "rev": 1, "v": "s"}]
    client = [{"id": "a", "rev": 1, "v": "c"}, {"id": "b", "rev": 2, "v": "c"}, {"id": "c", "rev": 1, "v": "c"}]
    m = summary.merge_client(server, client)
    assert [(r["id"], r["v"], bool(r.get("_unsaved"))) for r in m] == [("a", "s", False), ("b", "c", True), ("c", "c", True)]


# --- 起動 ------------------------------------------------------------------------


def test_announce_prints_url_and_writes_file(tmp_path, capsys):
    app = TrialApp(MockEngine(), load_configs(CONFIGS), tmp_path, version="t", examples=load_examples(EXAMPLES))
    trial_server.announce(app, "http://127.0.0.1:8766/")
    assert "http://127.0.0.1:8766/" in capsys.readouterr().out.splitlines()
    assert (tmp_path / "url.txt").read_text(encoding="utf-8") == "http://127.0.0.1:8766/\n"


def test_open_browser_failure(monkeypatch, capsys):
    def boom(url):
        raise OSError("no display")

    monkeypatch.setattr(trial_server.webbrowser, "open", boom)
    assert trial_server.open_browser("http://x/") is False
    assert "URL をブラウザのアドレス欄に貼って" in capsys.readouterr().out
    monkeypatch.setattr(trial_server.webbrowser, "open", lambda url: False)
    assert trial_server.open_browser("http://x/") is False


def test_bind_skips_used_port(tmp_path):
    app = TrialApp(MockEngine(), load_configs(CONFIGS), tmp_path, version="t", examples=load_examples(EXAMPLES))
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
