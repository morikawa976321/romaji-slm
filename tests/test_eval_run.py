import json

import pytest

from eval import datasets
from eval.converters import Result
from eval.report import aggregate, render_markdown, write_results
from eval.run import evaluate


class Echo:
    """入力のローマ字をそのまま返し、左文脈の長さを生成トークン数に見立てる固定の変換器。"""
    name = "echo"

    def convert(self, romaji, left_context=""):
        return self.convert_detailed(romaji, left_context).text

    def convert_detailed(self, romaji, left_context=""):
        return Result(romaji, 1, len(left_context), f"{left_context}|{romaji}")


def test_load_jsonl_shared_format(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"romaji": "a", "output": "あ", "left_context": "前", "meta": {"category": "x", "alternatives": ["ア"]}}) + "\n"
        + "\n" + json.dumps({"romaji": "b", "output": "び"}) + "\n", encoding="utf-8")
    items = datasets.load_jsonl(p)
    assert [i.refs for i in items] == [["あ", "ア"], ["び"]]
    assert (items[0].left_context, items[0].category) == ("前", "x")
    assert (items[1].left_context, items[1].category) == ("", "all")


def test_user_style_set_is_valid():
    items = datasets.load_set("user_style")
    assert len(items) == 40
    assert all(i.romaji and i.refs[0] and i.romaji == i.romaji.lower() for i in items)


def test_ajimee_with_and_without_context(tmp_path):
    raw = [
        {"index": "1", "context_text": "前の文", "input": "キョウハ", "expected_output": ["今日は"]},
        {"index": "2", "context_text": "", "input": "ニッポン", "expected_output": ["日本", "ニッポン"]},
    ]
    p = tmp_path / "items.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with_ctx = datasets.load_ajimee(True, p)
    no_ctx = datasets.load_ajimee(False, p)
    assert [i.romaji for i in with_ctx] == ["kyouha", "nipponn"]
    assert [i.left_context for i in with_ctx] == ["前の文", ""]
    assert [i.left_context for i in no_ctx] == ["", ""]
    # カテゴリは文脈の有無で決まり、渡す・渡さないに依らない
    assert [i.category for i in no_ctx] == ["with_context", "without_context"]
    assert with_ctx[1].refs == ["日本", "ニッポン"]


def test_unknown_set():
    with pytest.raises(ValueError):
        datasets.load_set("nope")


def test_evaluate_and_report(tmp_path):
    items = [
        datasets.Item("1", "あ", ["あ"], "ctx", "c1"),
        datasets.Item("2", "い。", ["い"], "", "c1"),
        datasets.Item("3", "う", ["え"], "", "c2"),
    ]
    rows = evaluate(Echo(), items)
    assert [r["exact"] for r in rows] == [True, False, False]
    assert [r["loose"] for r in rows] == [True, True, False]
    assert rows[0]["n_gen_tokens"] == 3 and rows[0]["ms"] >= 0

    agg = {a["category"]: a for a in aggregate(rows)}
    assert agg["c1"]["n"] == 2 and agg["c1"]["exact"] == 0.5 and agg["c1"]["loose"] == 1.0
    assert agg["ALL"]["n"] == 3 and agg["c2"]["min_cer"] == 1.0

    md = render_markdown("echo", "s", rows, {"cpu": "TestCPU", "torch_threads": 4})
    assert "cpu=TestCPU" in md and "| ALL | 3 |" in md and "255H" in md

    jsonl, mdp = write_results("echo", "file:a/b.jsonl", rows, {}, tmp_path)
    assert jsonl.name == "echo__file_a_b.jsonl.jsonl"
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 3
    assert mdp.exists()
