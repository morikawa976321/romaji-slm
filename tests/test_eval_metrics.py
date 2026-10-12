import pytest

from eval.metrics import cer, levenshtein, loose_normalize, min_cer, score


def test_levenshtein():
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "abd") == 1
    assert levenshtein("きょう", "今日") == 3
    assert levenshtein("abc", "") == 3


def test_cer_and_min_cer():
    assert cer("今日は", "今日は") == 0
    assert cer("abcd", "abxd") == 0.25
    assert cer("", "a") == 1  # 参照が空でも 0 割りしない
    assert min_cer(["abcd", "abxd"], "abxd") == 0
    assert min_cer(["abcd", "wxyz"], "abcc") == 0.25


def test_loose_normalize():
    assert loose_normalize("Ｇｉｔ Ｈｕｂ、ＯＫ！") == "githubok"   # 全角→半角・大小・空白・句読点
    assert loose_normalize("コーヒー") == loose_normalize("コヒ")    # 長音
    assert loose_normalize("明日〜会議") == "明日会議"
    assert loose_normalize("「A」(b)") == "ab"


def test_score():
    refs = ["明日の会議、何時からだっけ？", "明日の会議、何時からだっけ?"]
    assert score(refs, refs[1]) == {"exact": True, "loose": True, "min_cer": 0}
    s = score(refs, "明日の会議何時からだっけ")  # 記号だけが違う
    assert (s["exact"], s["loose"]) == (False, True) and s["min_cer"] > 0
    s = score(refs, "あしたのかいぎ")
    assert (s["exact"], s["loose"]) == (False, False)


def test_loose_does_not_ignore_kanji_diff():
    assert not score(["今日"], "京")["loose"]


@pytest.mark.parametrize("a, b", [("ABC", "abc"), ("ａ", "a")])
def test_case_and_width(a, b):
    assert loose_normalize(a) == loose_normalize(b)
