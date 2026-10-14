import json

import pytest

from eval.metrics import strip_punct
from eval.punct_eval import evaluate, main, marks, prf


def _row(id, ref, output, alts=()):
    return {"id": id, "category": "omit_punct", "romaji": f"r{id}", "refs": [ref, *alts], "output": output}


def test_strip_punct_keeps_other_marks():
    assert strip_punct("えーと、GitHub?（仮）。v1.0!") == "えーとGitHub（仮）v10"


def test_marks_positions():
    assert marks("今日は、晴れ。") == ("今日は晴れ", {(3, "読点"), (5, "句点")})
    assert marks("何？!") == ("何", {(1, "疑問符"), (1, "感嘆符")})


ROWS = [
    _row("a", "今日は、晴れ。", "今日は、晴れ。"),   # 陽性・完全
    _row("b", "今日は、晴れ。", "今日は晴れ"),       # 陽性・足りない（読点・句点）
    _row("c", "明日、雨。", "明日雨、"),             # 陽性・位置違い（読点が 2 → 3、句点なし）
    _row("d", "何時？", "何時?", alts=["何時?"]),    # 陽性・? と ？ は同じ種類
    _row("e", "コンシーラー", "コンシーラー。"),     # 陰性・挿入しすぎ
    _row("f", "万2887行", "万2887行"),               # 陰性・何も入れない
    _row("g", "犬、猫。", "猫、犬。"),               # 本文が違う
]


def test_evaluate_hand_computed():
    res = evaluate(ROWS)
    assert (res["n"], res["pos"], res["neg"]) == (7, 5, 2)
    assert res["body_ok"] == 6 and [r["id"] for r in res["body_mismatch"]] == ["g"]
    assert res["neg_inserted"] == 1
    # 正解の読点: a(3) b(3) c(2) → 3。出力の読点: a(3) c(3)。TP=1 (a)、FP=1 (c の 3)、FN=2 (b, c の 2)
    assert res["tp_fp_fn"]["読点"] == [1, 1, 2]
    # 正解の句点: a(5) b(5) c(3) → 3。出力の句点: a(5) e(6)。TP=1、FP=1 (e)、FN=2 (b, c)
    assert res["tp_fp_fn"]["句点"] == [1, 1, 2]
    assert res["tp_fp_fn"]["疑問符"] == [1, 0, 0]
    assert res["tp_fp_fn"]["感嘆符"] == [0, 0, 0]
    assert res["final_period"] == [1, 3] and res["mid_comma"] == [1, 3]
    assert [r["id"] for r in res["over"]] == ["e"]
    assert [r["id"] for r in res["under"]] == ["b"]
    assert [r["id"] for r in res["misplaced"]] == ["c"]


def test_prf():
    assert prf(1, 1, 2) == pytest.approx((0.5, 1 / 3, 0.4))
    assert prf(0, 0, 0) == (None, None, None)
    assert prf(0, 1, 1) == (0.0, 0.0, 0.0)


def test_main_filters_category(tmp_path, capsys):
    p = tmp_path / "r.jsonl"
    rows = ROWS + [{**_row("x", "あ。", "あ"), "category": "clean"}]
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    main([str(p), "--show", "1"])
    out = capsys.readouterr().out
    assert "件数 7（陽性 5 / 陰性 2）" in out
    assert "本文の保持: 85.7%（6/7）" in out
    assert "不要な挿入率（陰性のうち出力に句読点）: 50.0%（1/2）" in out
    assert "| 読点 | 3 | 2 | 50.0% | 33.3% | 40.0% |" in out
    assert "| 全体 | 7 | 5 | 60.0% | 42.9% | 50.0% |" in out
    assert "## 挿入しすぎ（1 件中 1 件）" in out
    main([str(p), "--category", "all"])
    assert "件数 8" in capsys.readouterr().out
