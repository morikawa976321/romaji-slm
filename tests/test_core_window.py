"""候補ウィンドウ（core/window.py）: 並べ方の規則と、1 位の読みの別の書き方の集め方。

並べ方は手で組んだ Pool で、別の書き方は極小 GPT-2 と手書きの辞書（tests/test_core_constrained.py と同じもの）で確かめる。
"""

import types

import pytest

from core.window import ORDERS, Entry, Pool, arrange


def _e(text, score, kind="same", edited=None):
    return Entry(text, score, kind, "", kind == "edit" if edited is None else edited)


def _pool(entries, first="行く", hira="いく"):
    return Pool(_e(first, -13.0, "first", False), _e(hira, float("-inf"), "hira", False) if hira else None, entries)


# iku の実測に似せた候補（読みの候補は点数順、別の書き方は点数順）
ENTRIES = [_e("以降", -23.7, "edit"), _e("行き", -24.9, "edit"), _e("奥", -25.8, "edit"), _e("肉", -26.0, "edit"),
           _e("インク", -26.2, "edit"), _e("育", -17.3), _e("イク", -17.7), _e("幾", -18.6), _e("逝く", -19.8),
           _e("いく", -12.6)]


def texts(win):
    return [e.text for e in win]


def test_reading_ignores_alternatives():
    # 置き換える前の並べ方: 読みの候補だけを点数順に 4 件、最後に 1 位の読みのひらがな
    assert texts(arrange(_pool(ENTRIES), "reading")) == ["行く", "以降", "行き", "奥", "肉", "いく"]


def test_same_first_and_merge_put_same_reading_first():
    assert texts(arrange(_pool(ENTRIES), "same_first")) == ["行く", "育", "イク", "幾", "逝く", "いく"]
    assert texts(arrange(_pool(ENTRIES), "merge")) == ["行く", "育", "イク", "幾", "逝く", "いく"]


def test_quota_keeps_edit_slots():
    assert texts(arrange(_pool(ENTRIES), "quota", edit_slots=1)) == ["行く", "育", "イク", "幾", "以降", "いく"]
    assert texts(arrange(_pool(ENTRIES), "quota", edit_slots=2)) == ["行く", "育", "イク", "以降", "行き", "いく"]
    # 直した読みが点数で入っているなら、枠の分だけ足さない
    es = [_e("以降", -14.0, "edit"), _e("育", -17.3), _e("イク", -17.7), _e("幾", -18.6), _e("逝く", -19.8)]
    assert texts(arrange(_pool(es), "quota", edit_slots=1)) == ["行く", "以降", "育", "イク", "幾", "いく"]


@pytest.mark.parametrize("order", ORDERS)
def test_window_shape(order):
    for hira in ("いく", "行く", ""):
        win = arrange(_pool(ENTRIES + [_e("行く", -15.0), _e("育", -30.0, "edit")], hira=hira), order, n=6)
        t = texts(win)
        assert t[0] == "行く" and len(t) == len(set(t)) and len(t) <= 6
        if hira and hira != "行く":
            assert t[-1] == hira
        if order != "reading":
            assert len(t) == 6   # ひらがなが 1 位と同じ・無いときは、その枠も候補で埋める


def test_width_follows_first():
    from core.window import _align_width
    assert _align_width("骨董品300点", "骨董品３００点が") == "骨董品３００点"
    assert _align_width("３月", "3月に") == "3月"
    assert _align_width("300と３", "300と３") == "300と３"   # 1 位が両方の幅を使っていれば触らない
    assert _align_width("三百", "骨董品") == "三百"
    assert _align_width("了解!有難う。", "了解！ありがとう。") == "了解！有難う。"
    assert _align_width("v1.2で。", "v1.2で。") == "v1.2で。"


def test_look_alike_duplicates_share_a_slot():
    es = [_e("了解!ありがとう。", -15.0, "typed"), _e("了解！有難う。", -16.0), _e("超会！ありがとう。", -20.0, "edit")]
    pl = Pool(_e("了解！ありがとう。", -13.0, "first", False), _e("りょうかい！ありがとう。", float("-inf"), "hira", False), es)
    assert texts(arrange(pl, "merge")) == ["了解！ありがとう。", "了解！有難う。", "超会！ありがとう。", "りょうかい！ありがとう。"]
    assert "了解!ありがとう。" in texts(arrange(pl, "reading"))   # reading は置き換える前と同じ（文字列の一致だけで除く）


def test_hira_look_alike_of_first_does_not_take_a_slot():
    es = [_e("有難う!", -15.0), _e("在り難う!", -16.0), _e("アリガトウ!", -17.0), _e("有り難う!", -18.0),
          _e("蟻が十!", -19.0)]
    pl = Pool(_e("ありがとう!", -13.0, "first", False), _e("ありがとう！", float("-inf"), "hira", False), es)
    t = texts(arrange(pl, "merge"))
    assert "ありがとう！" not in t and len(t) == 6
    assert texts(arrange(pl, "reading")) == ["ありがとう!", "ありがとう！"]   # reading は置き換える前と同じ


def _fake_pipeline(cands, rendered, en_case="typed"):
    return types.SimpleNamespace(candidates=lambda romaji, ctx: cands, _render_best=lambda best, romaji, ctx: rendered,
                                 en_case=en_case, backend=None)


def test_pool_keeps_first_when_alternatives_fail(monkeypatch):
    import core.window as w

    def boom(*a, **k):
        raise RuntimeError("out of memory")
    monkeypatch.setattr(w, "alternatives", boom)
    cands = [{"text": "行く", "score": -13.0, "reading": "イク", "reading_obj": None, "bounds": (), "edits": []},
             {"text": "以降", "score": -23.7, "reading": "イコウ", "reading_obj": None, "bounds": (), "edits": ["+o"]}]
    pl = w.pool(_fake_pipeline(cands, "行く"), "iku")
    assert texts(arrange(pl, "merge")) == ["行く", "以降", "いく"]
    with pytest.raises(RuntimeError):   # 評価（eval/window_eval.py）は strict で失敗を見えるようにする
        w.pool(_fake_pipeline(cands, "行く"), "iku", strict=True)


def test_end_mark_keeps_registered_en_at_the_end():
    # 1 位が文末に「。」を補ったとき、末尾が英語区間の候補も登録表記に描画する（en_case=registered）
    from core.types import Reading, Seg
    from core.window import pool
    r = Reading((Seg("kana", 0, 5, "テンキ"), Seg("en", 5, 11, "github", ("GitHub", "github"))))
    cands = [{"text": "天気github", "score": -10.0, "reading": "テンキgithub", "reading_obj": r, "bounds": (2,), "edits": []},
             {"text": "転機github", "score": -12.0, "reading": "テンキgithub", "reading_obj": r, "bounds": (2,), "edits": []}]
    pl = pool(_fake_pipeline(cands, "天気GitHub。", "registered"), "tenkigithub", alts=False)
    assert [e.text for e in pl.entries] == ["転機GitHub。"]


# ---- 別の書き方（極小モデル） ----

pytest.importorskip("torch")
pytest.importorskip("transformers")

from test_core_constrained import ENTRIES as DIC_ENTRIES, POS, _make_tiny  # noqa: E402

from core.kanakanji import Dictionary  # noqa: E402
from core.select import ZenzScorer  # noqa: E402
from core.types import Reading, Seg  # noqa: E402
from core.window import alternatives  # noqa: E402


@pytest.fixture(scope="module")
def scorer(tmp_path_factory, torch_device):
    return ZenzScorer(_make_tiny(tmp_path_factory.mktemp("zenz-like-window"), 256), torch_device)


@pytest.fixture
def dic():
    m = [[1000] * len(POS) for _ in POS]
    m[0][0] = 0
    return Dictionary.from_entries(DIC_ENTRIES, m, POS)


def _p(scorer, dic, punct="insert", en_case="typed"):
    return types.SimpleNamespace(scorer=scorer, dic=dic, punct=punct, en_case=en_case, w_flu=1.0, w_read=4.0)


def _best(r, text):
    return {"reading_obj": r, "text": text, "reading": r.zenz_input(), "edits": [], "score": 0.0}


def test_logprobs_eos_per_sequence(scorer):
    prompts, conts = ["a", "b", "c"], ["今日", "天気", "京"]
    mixed = scorer.logprobs(prompts, conts, eos=[True, False, True])
    assert mixed[0] == pytest.approx(scorer.logprobs(prompts[:1], conts[:1], eos=True)[0], abs=1e-4)
    assert mixed[1] == pytest.approx(scorer.logprobs(prompts[1:2], conts[1:2], eos=False)[0], abs=1e-4)


def test_alternatives_same_reading(scorer, dic):
    r = Reading((Seg("kana", 0, 5, "テンキ"),))
    alts = alternatives(_p(scorer, dic), _best(r, "天気"))
    got = [a.text for a in alts]
    assert "転機" in got and "天気" not in got           # 1 位の文そのものは除く
    assert all(a.kind == "same" and a.reading == "テンキ" for a in alts)
    assert [a.score for a in alts] == sorted((a.score for a in alts), reverse=True)


def test_alternatives_follow_modes(scorer, dic):
    # 英字は打った綴りのまま（登録表記の GitHub を出さない）、記号は全角・半角の違いだけの候補を並べない
    r = Reading((Seg("en", 0, 6, "github", ("GitHub", "github")), Seg("kana", 6, 9, "テンキ"),
                 Seg("sym", 9, 10, ".", ("。", "."))))
    got = [a.text for a in alternatives(_p(scorer, dic), _best(r, "github天気。"))]
    assert got and all(t.startswith("github") for t in got)
    assert "github天気." not in got
    ja = [a.text for a in alternatives(_p(scorer, dic, punct="ja"), _best(r, "github天気。"))]
    assert all(not t.endswith(".") for t in ja)


def test_alternatives_registered_en_with_end_mark(scorer, dic):
    r = Reading((Seg("kana", 0, 5, "テンキ"), Seg("en", 5, 11, "github", ("GitHub", "github"))))
    got = [a.text for a in alternatives(_p(scorer, dic, en_case="registered"), _best(r, "天気github"), end="。")]
    assert "転機GitHub。" in got
