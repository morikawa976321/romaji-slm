from core.pipeline import CorePipeline


def _pipe(fluency, **kw):
    """モデルを読まずに句読点 B だけを動かす（自然さは fluency(文) で与える）。"""
    p = CorePipeline.__new__(CorePipeline)
    p.tau_comma, p.tau_end, p.tau_q = -0.5, -6.0, -5.0
    p.punct_end, p.comma_tail, p.comma_min_len = False, True, 10
    for k, v in kw.items():
        setattr(p, k, v)
    p.seen = []

    def fl(texts, left_context):
        p.seen.append(list(texts))
        return [fluency(t) for t in texts]

    p._fluency = fl
    return p


def _favor_comma(t):
    return 1.0 if "、" in t else 0.0


def test_short_text_gets_no_comma_and_is_not_scored():
    p = _pipe(_favor_comma)
    assert p.insert_punct("今日は晴れ", (2, 3)) == ("今日は晴れ", (2, 3))
    assert p.seen == []


def test_long_text_gets_comma():
    p = _pipe(_favor_comma)
    text = "明日の会議は何時から始まるのか"
    out, bounds = p.insert_punct(text, (5,))
    assert out == "明日の会議、は何時から始まるのか"
    assert bounds == (5, 6)


def test_comma_min_len_zero_keeps_old_behavior():
    p = _pipe(_favor_comma, comma_min_len=0)
    assert p.insert_punct("今日は晴れ", (3,))[0] == "今日は、晴れ"


def test_comma_position_is_scored_with_provisional_period():
    # 「。」が付くと読点なしが自然になる、という自然さ。仮の「。」で比べるので読点は付かず、出力に「。」も付かない
    p = _pipe(lambda t: (1.0 if "、" in t else 0.0) if not t.endswith("。") else (-1.0 if "、" in t else 0.0))
    text = "明日の会議は何時から始まるのか"
    assert p.insert_punct(text, (5,))[0] == text
    assert p.seen == [[text + "。", "明日の会議、は何時から始まるのか。"]]


def test_comma_tail_off_scores_without_period():
    p = _pipe(_favor_comma, comma_tail=False)
    assert p.insert_punct("明日の会議は何時から始まるのか", (5,))[0] == "明日の会議、は何時から始まるのか"
    assert p.seen == [["明日の会議は何時から始まるのか", "明日の会議、は何時から始まるのか"]]


def test_punct_end_does_not_add_a_second_period():
    p = _pipe(lambda t: 0.0, punct_end=True)
    assert p.insert_punct("明日の会議は何時から始まるのか", (5,))[0] == "明日の会議、は何時から始まるのか。"
    assert not any(t.endswith("。。") for texts in p.seen for t in texts)
