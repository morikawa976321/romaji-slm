import pytest

from core.numbers import kanji_numeral, number_prefixes, surfaces


@pytest.mark.parametrize("kana, value", [
    ("サンビャク", 300), ("ハッピャク", 800), ("ロッピャク", 600), ("イッセン", 1000), ("サンゼン", 3000),
    ("ジュウゴ", 15), ("ニジュウシチ", 27), ("センキュウヒャクハチジュウイチ", 1981), ("ニセンゴ", 2005),
    ("サンマンゴセン", 35000), ("ヨンジュウ", 40), ("ク", 9), ("ゼロ", 0),
])
def test_reads_whole_number(kana, value):
    assert (len(kana), value) in number_prefixes(kana)


def test_prefixes_stop_before_counter():
    # 「ジュウゴニチ」の先頭からは 10 と 15 が読め、助数詞は含めない
    assert {v for e, v in number_prefixes("ジュウゴニチ")} == {10, 15}


def test_no_ichi_before_small_units():
    # 「イチジュウ」「イチヒャク」とは読まない
    assert (5, 10) not in number_prefixes("イチジュウ")


def test_surfaces():
    assert kanji_numeral(325) == "三百二十五" and kanji_numeral(1000) == "千" and kanji_numeral(12000) == "一万二千"
    assert surfaces(15) == ["15", "１５", "十五"]
