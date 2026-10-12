import argparse
import json
from collections import Counter

import pytest

pytest.importorskip("sudachipy")
pytest.importorskip("sudachidict_core")

from data import build as B  # noqa: E402
from data.reading import align_zenz, analyze  # noqa: E402


def make_args(**kw) -> argparse.Namespace:
    d = dict(limit=10**9, max_records=10**9, seed=0, noise_rate=0.0, noise_max=2, max_chars=60,
             max_left=200, valid_ratio=0.02, test_ratio=0.02, keep_mismatch=False, out="",
             omit_punct_rate=0.0)
    d.update(kw)
    return argparse.Namespace(**d)


def test_omit_punct_drops_japanese_symbols_but_keeps_ascii_in_literals():
    out, _ = run({"input": "ショウ、ヲツカウ。", "output": "llama.cppを使う。", "left_context": ""}, omit_punct_rate=1.0)
    assert [s["romaji"] for s in out] == ["llama.cppwotsukau"]
    assert out[0]["output"] == "llama.cppを使う。" and out[0]["meta"]["omit_punct"] is True


def run(rec, **kw):
    stats = Counter()
    return B.process_record(rec, make_args(**kw), stats), stats


def test_english_words_typed_as_lowercase_original_spelling():
    out, _ = run({"input": "ニシテオイテ", "output": "GitHubにpushしておいて。", "left_context": ""})
    r = out[0]["romaji"]
    assert r.startswith("githubni") and r.endswith("teoite.")
    assert "push" in r and "GitHub" not in r
    assert out[0]["output"] == "GitHubにpushしておいて。"


def test_spaces_dropped_from_input_kept_in_output():
    out, _ = run({"input": "ヲサクセイシマシタ", "output": "pull requestを作成しました。", "left_context": ""})
    assert out[0]["romaji"].startswith("pullrequestwosakusei")
    assert out[0]["output"].startswith("pull request")


def test_symbols_and_digits():
    out, _ = run({"input": "ニセンニジュウヨネンダ", "output": "２０２４年だ！", "left_context": ""})
    assert out[0]["romaji"].startswith("2024") and out[0]["romaji"].endswith("!")
    assert out[0]["output"] == "２０２４年だ！"  # 出力は元の表記のまま


def test_reading_from_zenz_when_consistent_and_analyzer_for_alnum_mismatch():
    ok, _ = run({"input": "キョウワテンキ", "output": "今日は天気", "left_context": ""})
    assert ok[0]["meta"]["reading_from"] == "zenz"
    assert ok[0]["romaji"].startswith("kyouha")  # 入力は表記どおり（は → ha）
    bad, _ = run({"input": "ゼンゼンチガウ", "output": "GitHubにpush", "left_context": ""})
    assert bad[0]["meta"]["reading_from"] == "analyzer"


def test_pure_japanese_mismatch_dropped_unless_kept():
    rec = {"input": "ゼンゼンチガウ", "output": "今日は天気", "left_context": ""}
    out, stats = run(rec)
    assert out == [] and stats["excluded:reading_mismatch"] == 1
    out, _ = run(rec, keep_mismatch=True)
    assert out[0]["meta"]["reading_from"] == "analyzer"


def test_unmappable_symbol_segment_excluded():
    out, stats = run({"input": "テスト", "output": "●テスト", "left_context": ""})
    assert out == [] and stats["excluded:unmapped_symbol"] == 1


def test_split_long_sentence_and_left_context_chain():
    s1 = "今日は天気がいい。"
    long = "朝ごはんを食べて、" * 8 + "学校に行った。"  # 60 字超
    out, stats = run({"input": "", "output": s1 + long, "left_context": "前の文。"}, keep_mismatch=True)
    assert len(out) >= 3
    assert out[0]["output"] == s1 and out[0]["left_context"] == "前の文。"
    assert all(len(o["output"]) <= 60 for o in out)
    assert out[1]["left_context"] == "前の文。" + s1
    assert "".join(o["output"] for o in out) == s1 + long


def test_quotes_not_split_inside():
    out, _ = run({"input": "", "output": "「いいね。」と彼は言った。", "left_context": ""}, keep_mismatch=True)
    assert len(out) == 1


def test_over_long_piece_without_punctuation_dropped():
    out, stats = run({"input": "", "output": "あ" * 70, "left_context": ""}, keep_mismatch=True)
    assert out == [] and stats["excluded_segments:too_long"] == 1


def test_align_zenz_particles_literals_and_substitution():
    toks = analyze("私はGitHubを使う")
    assert align_zenz(toks, "ワタクシワオツカウ")[1] == 0  # 英字の読みは任意長を許す
    assert align_zenz(toks, "ワタクシワギットハブオツカウ")[1] == 0
    r, n = align_zenz(toks, "ワタシワオツカウ")  # 私＝ワタクシ と ワタシ の揺れは zenz 側に合わせる
    assert n == 1 and r[0].reading == "ワタシ"
    assert align_zenz(toks, "ゼンゼンチガウ") is None


def test_zenz_reading_used_in_typed_text():
    out, stats = run({"input": "ワタシワツカウ", "output": "私は使う", "left_context": ""})
    assert out[0]["romaji"].startswith("watashiha")
    assert stats["zenz_substituted_tokens"] == 1


def test_digits_keep_counter_reading():
    out, _ = run({"input": "ニセンニジュウヨネンダ", "output": "2024年だ", "left_context": ""})
    assert out[0]["romaji"].startswith("2024nen")


def test_split_is_deterministic_and_per_record():
    a = B.split_of("イ", "出", 0.02, 0.02)
    assert a == B.split_of("イ", "出", 0.02, 0.02)
    c = Counter(B.split_of(str(i), str(i), 0.02, 0.02) for i in range(20000))
    assert 250 < c["valid"] < 550 and 250 < c["test"] < 550


def test_build_cli_reproducible_and_contract(tmp_path):
    for name in ("a", "b"):
        assert B.main(["--source", "fixture", "--limit", "100", "--out", str(tmp_path / name), "--seed", "3"]) == 0
    lines = {n: "".join((tmp_path / n / f"{k}.jsonl").read_text(encoding="utf-8") for k in ("train", "valid", "test")) for n in "ab"}
    assert lines["a"] == lines["b"]
    rows = [json.loads(line) for line in lines["a"].splitlines()]
    assert len(rows) == 100
    for r in rows:
        assert set(r) == {"romaji", "output", "left_context", "meta"}
        assert set(r["meta"]) == {"source", "noise", "reading_from", "omit_punct"}
        assert r["meta"]["reading_from"] in ("zenz", "analyzer")
        assert r["romaji"] and all("!" <= c <= "~" and not c.isupper() for c in r["romaji"])
    assert any(r["meta"]["noise"] for r in rows)
    other = B.main(["--source", "fixture", "--limit", "100", "--out", str(tmp_path / "c"), "--seed", "4"])
    assert other == 0 and (tmp_path / "c" / "train.jsonl").read_text(encoding="utf-8") != (tmp_path / "a" / "train.jsonl").read_text(encoding="utf-8")
