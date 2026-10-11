import pytest

from eval.kana import kata_to_romaji
from eval.romaji_kana import romaji_to_katakana


@pytest.mark.parametrize("romaji, kana", [
    ("konnichiha", "コンニチハ"),
    ("kanna", "カンナ"),            # nn の後ろが母音: ン + ナ
    ("kannnai", "カンナイ"),        # nnn: ン + ナ
    ("kantan", "カンタン"),
    ("hon", "ホン"),
    ("honn", "ホン"),
    ("hon'ya", "ホンヤ"),
    ("kannya", "カンニャ"),
    ("kitte", "キッテ"),
    ("matcha", "マッチャ"),
    ("gakkou", "ガッコウ"),
    ("nippon", "ニッポン"),
    ("xtu", "ッ"), ("ltu", "ッ"), ("xtsu", "ッ"),
    ("shi", "シ"), ("si", "シ"), ("chi", "チ"), ("ti", "チ"), ("tsu", "ツ"), ("tu", "ツ"),
    ("fu", "フ"), ("hu", "フ"), ("ji", "ジ"), ("zi", "ジ"),
    ("sha", "シャ"), ("sya", "シャ"), ("cha", "チャ"), ("tya", "チャ"), ("cya", "チャ"),
    ("ja", "ジャ"), ("jya", "ジャ"), ("zya", "ジャ"),
    ("kyou", "キョウ"), ("ryuu", "リュウ"), ("nyuu", "ニュウ"),
    ("xya", "ャ"), ("lyu", "ュ"), ("xa", "ァ"),
    ("fa", "ファ"), ("fi", "フィ"), ("thi", "ティ"), ("dhi", "ディ"), ("twu", "トゥ"),
    ("wi", "ウィ"), ("we", "ウェ"), ("va", "ヴァ"), ("vu", "ヴ"), ("tsa", "ツァ"),
    ("sushi", "スシ"), ("ni-ha-o", "ニーハーオ"),
    ("a,i.", "ア、イ。"), ("ya?", "ヤ？"), ("a[i]", "ア「イ」"), ("a/i", "ア・イ"),
])
def test_romaji_to_katakana(romaji, kana):
    assert romaji_to_katakana(romaji) == kana


def test_unconvertible_letters_remain():
    # 子音だけが余る・数字・変換できない英字はそのまま
    assert romaji_to_katakana("k") == "k"
    assert romaji_to_katakana("2024") == "2024"
    assert romaji_to_katakana("qkaa") == "qカア"
    assert romaji_to_katakana("") == ""


def test_n_at_end_and_before_consonant():
    assert romaji_to_katakana("n") == "ン"
    assert romaji_to_katakana("anta") == "アンタ"


@pytest.mark.parametrize("kana", [
    "コンニチハ", "ガッコウ", "キョウ", "ショウガッコウ", "ファイル", "ティーム", "ウィンドウ",
    "ヴァイオリン", "セイネンシ", "ニッポン", "チャンス", "ジュウショ",
])
def test_roundtrip_with_kana_py(kana):
    # kana.py（カナ→ローマ字）の出力を戻すと元のカナに戻る
    assert romaji_to_katakana(kata_to_romaji(kana)) == kana


def test_roundtrip_n_before_vowel():
    # ン+母音・y は n' と書く。nn+母音 だと「ん」+な行に読まれて戻らない
    assert kata_to_romaji("ホンヤ") == "hon'ya"
    assert kata_to_romaji("ホンア") == "hon'a"
    assert kata_to_romaji("ホンナ") == "honna"
    assert kata_to_romaji("ホン") == "honn"
    for kana in ["ホンヤ", "ホンア", "ホンナ", "ホンニャ", "ホン", "ホンタ", "ニンイ", "ホン、"]:
        assert romaji_to_katakana(kata_to_romaji(kana)) == kana
