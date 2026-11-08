"""本人の語彙の加点（core/uservocab.py と core/constrained.py の加点つきの選び方）。"""

import pytest

from core import uservocab as uv
from core.constrained import LatticeMatcher, pick_with_bonus
from core.kanakanji import Dictionary
from core.types import Reading, Seg

POS = ["BOS/EOS,*,*,*,*,*,*", "名詞,一般,*,*,*,*,*", "助詞,係助詞,*,*,*,*,は"]
NOUN, WA = 1, 2
ENTRIES = [("きょう", NOUN, NOUN, 3000, "今日"), ("きょう", NOUN, NOUN, 4000, "京"), ("は", WA, WA, 500, "は")]


@pytest.fixture
def dic():
    m = [[1000] * len(POS) for _ in POS]
    m[0][0] = 0
    return Dictionary.from_entries(ENTRIES, m, POS)


def _kana(text: str) -> Reading:
    return Reading((Seg("kana", 0, len(text), text),))


def test_from_counts_uses_share_within_reading_with_prior():
    v = uv.from_counts({("すべて", "すべて"): 38, ("すべて", "全て"): 10, ("か", "化"): 50,
                        ("てまえ", "手前"): 1, ("きょう", "京"): 2})
    assert v.get("スベテ", "すべて") == pytest.approx(38 / 50)   # 読みはカタカナでも引ける
    assert v.get("すべて", "全て") == pytest.approx(10 / 50)
    assert v.get("か", "化") == 0.0           # 読みが一字の語は使わない
    assert v.get("てまえ", "手前") == 0.0     # 1 回しか出ない組は使わない
    assert v.get("きょう", "京") == pytest.approx(2 / 4)   # 回数の少ない読みは割り引く


def test_load_sums_rows_of_the_same_pair(tmp_path):
    p = tmp_path / "vocab.tsv"
    p.write_text("reading\tsurface\tpos\tunit\tcount\tunits\tbefore\n"
                 "きょう\t京\t名詞-固有名詞\tAC\t3\t3\t3\n"
                 "きょう\t京\t名詞-普通名詞\tA\t3\t2\t0\n", encoding="utf-8")
    assert uv.load(p).get("きょう", "京") == pytest.approx(6 / 8)
    assert not uv.load(tmp_path / "none.tsv")


def test_matcher_bonus_of_text_and_advance_acc(dic):
    v = uv.UserVocab({("キョウ", "京"): 0.8, ("キョウ", "今日"): 0.1})
    m = LatticeMatcher(_kana("キョウハ"), dic, vocab=v)
    assert m.bonus_of("京は") == pytest.approx(0.8)
    assert m.bonus_of("今日は") == pytest.approx(0.1)
    assert m.bonus_of("天気") == 0.0   # 出せない文
    # 語の最初のバイトを読んだ時点で加点が入る
    acc = m.advance_acc(m.start_acc(), "京".encode()[:1])
    assert max(acc.values()) == pytest.approx(0.8)
    assert LatticeMatcher(_kana("キョウハ"), dic).bonus == {}   # 語彙が無ければ加点の弧も無い


def test_pick_with_bonus_prefers_the_vocab_token_only_within_the_weight(dic):
    v = uv.UserVocab({("キョウ", "京"): 0.9})
    m = LatticeMatcher(_kana("キョウハ"), dic, vocab=v)
    tok_bytes = [b"x", "今日".encode(), "京".encode(), b""]
    eos = 3
    order = [(0, 1.0), (1, 0.0), (2, -0.5)]   # 0 はラティスに無い
    assert pick_with_bonus(order, m, m.start_acc(), 0.0, tok_bytes, eos)[0] == 1   # 加点なし = 最初の合法トークン
    assert pick_with_bonus(order, m, m.start_acc(), 0.3, tok_bytes, eos)[0] == 1   # 0.0 > -0.5 + 0.27
    t, acc = pick_with_bonus(order, m, m.start_acc(), 1.0, tok_bytes, eos)
    assert t == 2 and max(acc.values()) == pytest.approx(0.9)
    # 加点で逆転しえないほど低いトークンは比べない（w × BONUS_SPAN より下）
    assert pick_with_bonus([(1, 0.0), (2, -5.0)], m, m.start_acc(), 1.0, tok_bytes, eos)[0] == 1
