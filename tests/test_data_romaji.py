import random

import pytest

from data.noise import add_noise
from data.romaji import UnreadableKana, kana_to_romaji, romanize
from data.symbols import to_typed
from eval.kana import kata_to_romaji


def variants(kana: str, n: int = 400) -> set[str]:
    return {kana_to_romaji(kana, random.Random(i)) for i in range(n)}


def test_standard_spelling_matches_eval_side():
    for s in ["コンニチハ", "ガッコウ", "キョウト", "シュッチョウ", "ファイル", "ラーメン", "ティーム", "ウィキ"]:
        assert kana_to_romaji(s) == kata_to_romaji(s)


def test_variants_present():
    assert {"shi", "si"} <= variants("シ")
    assert {"chi", "ti"} <= variants("チ")
    assert {"tsu", "tu"} <= variants("ツ")
    assert {"fu", "hu"} <= variants("フ")
    assert {"ji", "zi"} <= variants("ジ")
    assert {"ja", "jya", "zya"} <= variants("ジャ")
    assert {"sha", "sya"} <= variants("シャ")


def test_small_kana_x_and_l():
    v = variants("ァ")
    assert v == {"xa", "la"}
    assert {"kya", "kixya", "kilya"} <= variants("キャ", 3000)


def test_hatsuon_nn_before_vowel_y_n_and_end():
    for kana, tail in [("ホンア", "a"), ("ホンヤ", "ya"), ("ホンナ", "na"), ("ホン", "")]:
        for v in variants(kana):
            assert v.startswith("ho") and v.endswith(tail)
            assert v.removeprefix("ho").startswith("nn"), v
    # 子音の前は n も nn もある
    assert {"honka", "honnka"} <= variants("ホンカ")


def test_hatsuon_before_literal_is_nn():
    for i in range(50):
        assert romanize([("kana", "ホン"), ("lit", "github")], random.Random(i)) == "honngithub"


def test_sokuon_doubles_consonant_with_rare_xtu():
    v = variants("ガッコウ", 2000)
    assert "gakkou" in v and {"gaxtukou", "galtukou"} & v
    assert max(kana_to_romaji("ガッコウ", random.Random(i)) == "gakkou" for i in range(20))
    # 次の綴りに合わせて重ねる
    assert {"cchi", "tti"} == {x for x in variants("ッチ") if x in ("cchi", "tti")}
    assert kana_to_romaji("ッ") == "xtu"
    assert kana_to_romaji("アッ") == "axtu"
    assert kana_to_romaji("イッア") == "ixtua"


def test_long_vowel_mark():
    assert kana_to_romaji("ラーメン") == "ra-menn"


def test_unreadable_kana_raises():
    with pytest.raises(UnreadableKana):
        kana_to_romaji("漢字")


def test_symbol_mapping():
    assert to_typed("、。ー！？「」・：") == ",.-!?[]/:"
    assert to_typed("１２３") == "123"
    assert to_typed("ＧＰＵ") == "gpu"
    assert to_typed("pull request") == "pullrequest"
    assert to_typed("（ａ）") == "(a)"
    assert to_typed("●") is None
    assert to_typed("あ") is None


def test_noise_reproducible_and_recorded():
    a = add_noise("konnnichiha", random.Random(1), rate=1.0)
    b = add_noise("konnnichiha", random.Random(1), rate=1.0)
    assert a == b
    text, notes = a
    assert text != "konnnichiha" and 1 <= len(notes) <= 2


def test_noise_rate_and_charset():
    rng = random.Random(0)
    n_noisy = 0
    for i in range(2000):
        text, notes = add_noise("watashihagakkoudebenkyoushiteimasu,ashita.", rng, rate=0.3)
        n_noisy += bool(notes)
        assert text and all("!" <= c <= "~" and not c.isupper() for c in text)
        assert bool(notes) == (text != "watashihagakkoudebenkyoushiteimasu,ashita.")
    assert 0.25 < n_noisy / 2000 < 0.35


def test_noise_ops_all_occur_and_keep_symbols():
    ops = set()
    for i in range(500):
        text, notes = add_noise("a,b.c-dgfhjk", random.Random(i), rate=1.0)
        ops |= {n.split(":")[0] for n in notes}
        assert text.count(",") == 1 and text.count(".") == 1 and text.count("-") == 1
    assert ops == {"sub", "drop", "insert", "swap"}


def test_noise_skips_short():
    assert add_noise("ab", random.Random(0), rate=1.0) == ("ab", [])
