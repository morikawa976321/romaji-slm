"""本人の語彙の加点（core/uservocab.py と core/constrained.py の加点つきの選び方）。"""

import pytest

from core import uservocab as uv
from core.constrained import LatticeMatcher, pick_with_bonus
from core.kanakanji import Dictionary
from core.types import Reading, Seg

POS = ["BOS/EOS,*,*,*,*,*,*", "名詞,一般,*,*,*,*,*", "助詞,係助詞,*,*,*,*,は"]
NOUN, WA = 1, 2
ENTRIES = [("きょう", NOUN, NOUN, 3000, "今日"), ("きょう", NOUN, NOUN, 4000, "京"), ("は", WA, WA, 500, "は"),
           ("ほしい", NOUN, NOUN, 3000, "欲しい"), ("ほし", NOUN, NOUN, 3000, "欲し"), ("い", NOUN, NOUN, 3000, "い"),
           ("ほしいもの", NOUN, NOUN, 3000, "欲しい物"), ("はいる", NOUN, NOUN, 3000, "はいる"),
           ("はいる", NOUN, NOUN, 3000, "入る"), ("いる", NOUN, NOUN, 3000, "いる")]


@pytest.fixture
def dic():
    m = [[1000] * len(POS) for _ in POS]
    m[0][0] = 0
    return Dictionary.from_entries(ENTRIES, m, POS)


def _kana(text: str) -> Reading:
    return Reading((Seg("kana", 0, len(text), text),))


def test_from_counts_lowers_the_spellings_the_user_does_not_use_within_one_word():
    v = uv.from_counts({("すべて", "すべて", "全て"): 30, ("すべて", "全て", "全て"): 10, ("すべて", "総て", "全て"): 0,
                        ("か", "化", "化"): 50, ("てまえ", "手前", "手前"): 1, ("てまえ", "てまえ", "手前"): 0,
                        ("きょう", "京", "京"): 2, ("きょう", "今日", "今日"): 40})
    assert v.get("スベテ", "すべて") == 0.0                       # 本人がいちばん使う書き方は 0（読みはカタカナでも引ける）
    assert v.get("すべて", "全て") == pytest.approx((10 - 30) / 42)
    assert v.get("すべて", "総て") == pytest.approx(-30 / 42)     # 辞書にあって本人が使わない書き方ほど下がる
    assert v.get("か", "化") == 0.0                               # 読みが一字の語は使わない
    assert v.get("てまえ", "てまえ") == 0.0                       # いちばん使う書き方が 1 回だけの組は使わない
    assert v.get("きょう", "京") == 0.0 and v.get("きょう", "今日") == 0.0   # 別の語どうしは比べない


def test_from_counts_keeps_the_higher_score_when_a_spelling_is_in_two_words():
    v = uv.from_counts({("いい", "いい", "良い"): 30, ("いい", "良い", "良い"): 3,
                        ("いい", "言い", "言う"): 10, ("いい", "いい", "言う"): 2})
    assert v.get("いい", "いい") == 0.0   # 言う の組では負でも、良い の組でいちばん使う書き方
    assert v.get("いい", "良い") < 0 and v.get("いい", "言い") == 0.0


def _tsv(*rows) -> str:
    return "".join(chr(9).join(map(str, r)) + chr(10) for r in rows)


def test_load_sums_rows_of_the_same_word_and_ignores_old_tables(tmp_path):
    p = tmp_path / "vocab.tsv"
    p.write_text(_tsv(["reading", "surface", "lemma", "pos", "unit", "count", "units", "before"],
                      ["ほしい", "ほしい", "欲しい", "形容詞-非自立可能", "AC", 3, 3, 3],
                      ["ほしい", "ほしい", "欲しい", "形容詞-一般", "A", 3, 2, 0],
                      ["ほしい", "欲しい", "欲しい", "形容詞-非自立可能", "V", 0, 0, 0]), encoding="utf-8")
    assert uv.load(p).get("ほしい", "欲しい") == pytest.approx(-6 / 8)
    assert not uv.load(tmp_path / "none.tsv")
    old = tmp_path / "old.tsv"
    old.write_text(_tsv(["reading", "surface", "pos", "unit", "count", "units", "before"],
                        ["ほしい", "欲しい", "形容詞", "AC", 3, 3, 3]), encoding="utf-8")
    assert not uv.load(old)   # 正規形の列の無い表は使わない（読みだけで束ねると別の語を寄せる）


def test_matcher_bonus_of_text_and_advance_acc(dic):
    v = uv.UserVocab({("キョウ", "京"): -0.8, ("キョウ", "今日"): -0.1})
    m = LatticeMatcher(_kana("キョウハ"), dic, vocab=v)
    assert m.bonus_of("京は") == pytest.approx(-0.8)
    assert m.bonus_of("今日は") == pytest.approx(-0.1)
    assert m.bonus_of("きょうは") == 0.0   # 表に無い書き方（かなの弧）は 0
    assert m.bonus_of("天気") == 0.0   # 出せない文
    assert m.bonus_max == 0.0
    # 語の最初のバイトを読んだ時点で点が入る（京・今 は最初のバイトが同じなので、2 バイト目で分かれる）
    acc = m.advance_acc(m.start_acc(), "京".encode()[:2])
    assert max(acc.values()) == pytest.approx(-0.8)
    assert max(m.advance_acc(m.start_acc(), "京".encode()[:1]).values()) == pytest.approx(-0.1)
    assert LatticeMatcher(_kana("キョウハ"), dic).bonus == {}   # 語彙が無ければ加点の弧も無い


def test_pick_with_bonus_avoids_the_lowered_spelling_only_within_the_weight(dic):
    v = uv.UserVocab({("キョウ", "今日"): -0.9})
    m = LatticeMatcher(_kana("キョウハ"), dic, vocab=v)
    tok_bytes = [b"x", "今日".encode(), "京".encode(), b""]
    eos = 3
    order = [(0, 1.0), (1, 0.0), (2, -0.5)]   # 0 はラティスに無い
    assert pick_with_bonus(order, m, m.start_acc(), 0.0, tok_bytes, eos)[0] == 1   # 重み 0 = 最初の合法トークン
    assert pick_with_bonus(order, m, m.start_acc(), 0.3, tok_bytes, eos)[0] == 1   # 0.0 − 0.27 > −0.5
    t, acc = pick_with_bonus(order, m, m.start_acc(), 1.0, tok_bytes, eos)
    assert t == 2 and max(acc.values()) == 0.0
    # 点で逆転しえないほど低いトークンは比べない（最初の合法トークンの点を引いた値より下）
    assert pick_with_bonus([(1, 0.0), (2, -5.0)], m, m.start_acc(), 1.0, tok_bytes, eos)[0] == 1


def test_pick_with_bonus_still_handles_positive_scores(dic):
    v = uv.UserVocab({("キョウ", "京"): 0.9})
    m = LatticeMatcher(_kana("キョウハ"), dic, vocab=v)
    tok_bytes = [b"x", "今日".encode(), "京".encode(), b""]
    assert pick_with_bonus([(1, 0.0), (2, -0.5)], m, m.start_acc(), 1.0, tok_bytes, 3)[0] == 2
    assert pick_with_bonus([(1, 0.0), (2, -5.0)], m, m.start_acc(), 1.0, tok_bytes, 3)[0] == 1   # 2 × 0.9 より下


def test_lowered_word_cannot_escape_through_shorter_words(dic):
    v = uv.UserVocab({("ホシイ", "欲しい"): -0.7})
    m = LatticeMatcher(_kana("ホシイ"), dic, vocab=v)
    assert m.bonus_of("欲しい") == pytest.approx(-0.7)   # 欲し＋い と区切っても同じ減点
    assert m.bonus_of("ほしい") == 0.0
    m = LatticeMatcher(_kana("ホシイモノ"), dic, vocab=v)
    assert m.bonus_of("欲しい物") == 0.0                  # 長い語として出す道は減らない

    # かなだけの頭（は）には減点を広げない。助詞の は ＋ いる と見分けられないため
    m = LatticeMatcher(_kana("ハイル"), dic, vocab=uv.UserVocab({("ハイル", "はいる"): -0.6}))
    assert m.bonus_of("はいる") == 0.0 and m.bonus_of("入る") == 0.0
