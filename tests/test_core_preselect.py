import json
from pathlib import Path

import pytest

from core.english import lexicon
from core.kanakanji import MOZC_DIR

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not lexicon().words or not (MOZC_DIR / "connection_single_column.txt").exists(),
                                reason="英単語リストか Mozc の辞書（core.fetch_mozc）が無い")


@pytest.fixture(scope="module")
def dic():
    from core.kanakanji import Dictionary

    return Dictionary.load()


def test_preselect_keeps_first_and_hypotheses(dic):
    from core.preselect import preselect
    from core.reading import readings

    for keys in ("noiraiakuuyouni", "githubnipushshitejrnoapiwotamesu", "kyouhaosokunarisoudalara,sakinitabetwoite."):
        rs = readings(keys, 30)
        top = preselect(rs, dic, 5)
        assert top[0] == rs[0]
        assert len(top) == 5 and len({r.segs for r in top}) == 5
        assert any(not r.edits for r in top)  # 「直さない」を残す


def test_conv_cost_matches_convert(dic):
    from core.preselect import conv_cost
    from core.kanakanji import convert
    from core.reading import readings

    r = readings("kaizentenwokajougakidedashite.", 1)[0]
    assert conv_cost(r, dic) == convert(r, dic, 1)[0].cost


def test_preselect_all_raw_fills_slots(dic):
    from core.preselect import preselect
    from core.reading import readings

    # どの候補も辞書に無い英字列（sft）を含む入力でも5件埋める
    assert len(preselect(readings("sftnoatonidpowokakeru", 30), dic, 5)) == 5


def test_preselect_ignores_uppercase_span_for_raw_quota(dic):
    from core.preselect import _has_raw, preselect
    from core.reading import readings

    assert not _has_raw(readings("UInoseiyaku", 1)[0])
    # 大文字の区間を英字列として数えると、全候補が英字列あり扱いになって sft を soft に直す候補が落ちる
    rs = readings("UIsftnoatonidpowokakeru", 30)
    assert "UIsoftノアトニドヲカケル" in [r.zenz_input() for r in preselect(rs, dic, 5)]


def test_typo_hard_set_format():
    from eval.romaji_kana import romaji_to_katakana
    from eval.reading_bench import norm

    rows = [json.loads(l) for l in (ROOT / "eval/sets/typo_hard.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert 60 <= len(rows) <= 100
    real = json.loads((ROOT / "eval/sets/user_real.jsonl").read_text(encoding="utf-8").splitlines()[0])["romaji"]
    assert rows[0]["romaji"] == real
    for d in rows:
        m = d["meta"]
        assert m["category"] in ("multi", "extra_valid")
        assert d["romaji"] != m["clean_romaji"]
        assert norm(romaji_to_katakana(m["clean_romaji"])) == norm(d["reading"])
        if m["category"] == "extra_valid":
            assert len(d["romaji"]) == len(m["clean_romaji"]) + 1
