import pytest

pytest.importorskip("sudachipy")
pytest.importorskip("sudachidict_core")

from tools import vocab as v  # noqa: E402


def test_words_counts_long_and_short_units_and_skips_counters_and_alnum():
    ws, unknown = v.words("選挙管理委員会の資料を3回cursorで見た")
    got = {(r, s, unit) for r, s, _, _, unit in ws}
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
    n, units, before = table[("しりょう", "資料", "資料", "名詞-普通名詞", "AC")]
    assert (n, units, before) == (3, 2, 1)


def test_words_carry_the_normalized_form_of_the_word():
    ws, _ = v.words("すべて欲しい")
    lemma = {s: lm for _, s, lm, _, _ in ws}
    assert lemma["すべて"] == "全て"   # 書き分けは正規形で同じ語にまとまる
    assert lemma["欲しい"] == "欲しい"


class _Dic:
    def __init__(self, entries):
        self.entries = entries

    def lookup(self, kata):
        return [(0, 0, 0, s) for s in self.entries.get(kata, ())]


def test_add_variants_adds_unused_spellings_of_the_same_word_only():
    table = {("すべて", "すべて", "全て", "名詞-普通名詞", "AC"): [5, 5, 0]}
    n = v.add_variants(table, _Dic({"スベテ": ["全て", "総て", "滑べて"]}))
    one = {("いち", "一", "1", "名詞-数詞", "AC"): [5, 5, 0]}   # SudachiPy の 一・１ の正規形は 1
    v.add_variants(one, _Dic({"イチ": ["１", "壱"]}))
    assert "１" not in {s for (_, s, _, _, u) in one if u == "V"}   # 英数字の書き方は足さない
    added = {s for (_, s, _, _, unit) in table if unit == "V"}
    assert "全て" in added and "スベテ" not in added   # 辞書の書き分けは足し、カタカナは同じ語にならないので足さない
    assert "滑べて" not in added                        # 別の語・解析で 1 語にならないものは足さない
    assert n == len(added)
    assert all(table[k] == [0, 0, 0] for k in table if k[4] == "V")
