import pytest

pytest.importorskip("sudachipy")
pytest.importorskip("sudachidict_core")

from tools import vocab as v  # noqa: E402


def test_words_counts_long_and_short_units_and_skips_counters_and_alnum():
    ws, unknown = v.words("選挙管理委員会の資料を3回cursorで見た")
    got = {(r, s, unit) for r, s, _, unit in ws}
    assert ("せんきょかんりいいんかい", "選挙管理委員会", "C") in got
    assert ("せんきょ", "選挙", "A") in got
    assert ("しりょう", "資料", "AC") in got
    assert ("を", "を", "AC") in got  # 助詞は表記どおりの読み（打つ綴りと同じ）
    assert not any(s == "回" for _, s, _ in got)  # 数字の直後の助数詞は数えない
    assert not any("cursor" in s for _, s, _ in got)
    assert unknown == []


def test_count_keeps_before_romaji_slm_separately():
    table, _ = v.count([
        {"text": "資料を見た", "date": "2026-10-01"},
        {"text": "資料と資料", "date": "2026-11-01"},
    ])
    n, units, before = table[("しりょう", "資料", "名詞-普通名詞", "AC")]
    assert (n, units, before) == (3, 2, 1)
