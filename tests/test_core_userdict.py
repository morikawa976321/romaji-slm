import pytest

from core.english import lexicon, set_user_words, user_bonus
from core.kanakanji import Dictionary
from core.reading import readings
from core.userdict import EN_BONUS_MAX, parse


def test_parse_kinds_and_errors():
    d = parse("# c\n\nen\tSupabase\nen\tmain\t\t9\nword\tでんあつ\t電圧\nword\tちみ\t魑魅\t3000\nfix\tdenatu\tdennatu\nbad\tx\nfix\ta\ta\n")
    assert d.en == {"supabase": ("supabase", 0.0), "main": ("main", EN_BONUS_MAX)}
    assert d.words == [("でんあつ", "電圧", 3500), ("ちみ", "魑魅", 3000)]
    assert d.fixes == [("denatu", "dennatu")]
    assert len(d.errors) == 2


@pytest.fixture
def user_words():
    yield
    set_user_words({})   # 他の試験に持ち越さない


def test_user_english_word_becomes_an_english_arc(user_words):
    assert lexicon().get("supabase") is None
    set_user_words({"supabase": ("supabase", 0.5)})
    assert lexicon().get("supabase") is not None and user_bonus("supabase") == 0.5
    assert any(s.kind == "en" and s.text == "supabase" for r in readings("supabasede", n=5) for s in r.segs)


def test_user_words_are_merged_into_the_index():
    dic = Dictionary.from_entries([("でんあつ", 1, 1, 4000, "電圧")], [[0, 0], [0, 0]], ["BOS", "名詞,一般,*,*,*,*,*"])
    dic.add_user_words([("でんあつ", "田圧", 3000), ("ちみもうりょう", "魑魅魍魎", 3000)])
    assert {s for *_, s in dic.lookup("デンアツ")} == {"電圧", "田圧"}
    assert dic.lookup("チミモウリョウ")[0][3] == "魑魅魍魎"
    assert "チミモウリョウ" in dic.index.prefixes("チミモウリョウデ")
    dic.add_user_words([("あ", "亜", 3000)])   # 入れ直しても Mozc の索引は残る
    assert {s for *_, s in dic.lookup("デンアツ")} == {"電圧"}
