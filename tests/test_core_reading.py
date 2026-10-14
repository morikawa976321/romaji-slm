import pytest

from core.english import lexicon
from core.reading import readings
from core.romaji_table import TABLE

pytestmark = pytest.mark.skipif(not lexicon().words, reason="英単語リストが読めない")


def top(keys, n=20, **kw):
    return readings(keys, n, **kw)


def inputs(rs):
    return [r.zenz_input() for r in rs]


@pytest.mark.parametrize("keys, kana", [
    ("konnnichiha", "コンニチハ"),
    ("kanna", "カンア"),  # nn は常に「ン」
    ("kannna", "カンナ"),
    ("hon'ya", "ホンヤ"),
    ("honnya", "ホンヤ"),
    ("kantan", "カンタン"),
    ("kitte", "キッテ"),
    ("matcha", "マッチャ"),
    ("sya", "シャ"), ("tya", "チャ"), ("cya", "チャ"), ("zya", "ジャ"), ("jya", "ジャ"),
    ("si", "シ"), ("ti", "チ"), ("tu", "ツ"), ("hu", "フ"), ("zi", "ジ"),
    ("fa", "ファ"), ("thi", "ティ"), ("dhi", "ディ"), ("twu", "トゥ"), ("wi", "ウィ"), ("va", "ヴァ"),
    ("xtu", "ッ"), ("ltu", "ッ"), ("xa", "ァ"), ("lyu", "ュ"),
    ("ra-men", "ラーメン"),
])
def test_clean_romaji_is_first_without_edits(keys, kana):
    r = top(keys, 5)[0]
    assert r.zenz_input() == kana
    assert r.edits == ()


def test_nn_vowel_also_read_as_n_row():
    rs = top("jikkennokekka")
    assert rs[0].zenz_input() == "ジッケンオケッカ"
    assert "ジッケンノケッカ" in inputs(rs[:5])
    alt = next(r for r in rs if r.zenz_input() == "ジッケンノケッカ")
    assert "nn" in alt.breakdown and alt.edits == ()


def test_english_and_surfaces():
    rs = top("githubnipushshiteoite")
    r = rs[0]
    assert r.zenz_input() == "githubニpushシテオイテ"
    en = [s for s in r.segs if s.kind == "en"]
    assert [s.text for s in en] == ["github", "push"]
    assert en[0].surfaces[0] == "GitHub" and "github" in en[0].surfaces
    assert (en[0].start, en[0].end) == (0, 6)


def test_english_kept_in_nbest_when_romaji_also_parses():
    rs = top("konoissuehamadashuuseishinaide.")
    assert "コノissueハマダシュウセイシナイデ。" in inputs(rs[:5])
    assert "コノイッスエハマダシュウセイシナイデ。" in inputs(rs[:5])


def test_english_typo_goes_through_edit():
    rs = top("githibnipushshiteoite")
    hit = [r for r in rs[:5] if r.zenz_input() == "githubニpushシテオイテ"]
    assert hit and hit[0].edits[0].kind == "sub" and hit[0].edits[0].detail == "i>u"


def test_chunk_with_dot_and_digits():
    r = top("llama.cppdemoderuwougokasu.")[0]
    assert r.segs[0].kind == "en" and r.segs[0].text == "llama.cpp"
    r = top("claude.mdnikomitto")[0]
    assert r.segs[0].text == "claude.md"
    assert any(s.kind == "en" and s.text == "gpt-2" for s in top("gpt-2wotsukau")[0].segs)
    assert any(s.kind == "en" and s.text == "v3.1" for s in top("v3.1nishita")[0].segs)


def test_number_owns_inner_dot():
    r = top("3.14dearu")[0]
    assert r.segs[0].kind == "num" and r.segs[0].text == "3.14"
    r = top("pythonnoba-jonwo3.12niageta.")[0]
    assert r.zenz_input() == "pythonノバージョンヲ3.12ニアゲタ。"


@pytest.mark.parametrize("sym, surfaces", [
    (",", ("、", ",")), (".", ("。", ".")), ("?", ("？", "?")), ("!", ("！", "!")),
    ("[", ("「", "[")), ("]", ("」", "]")), ("/", ("・", "/")), (":", ("：", ":")),
])
def test_symbols(sym, surfaces):
    r = top(f"a{sym}i")[0]
    seg = next(s for s in r.segs if s.kind == "sym")
    assert seg.text == sym and seg.surfaces == surfaces


def test_short_english_only_from_allow_list():
    assert any(s.kind == "en" and s.text == "ok" for s in top("ok,sorededeployshite.")[0].segs)
    # 2文字の一般語（no, to, so…）は英語区間にしない
    for r in top("sonotokinosoto"):
        assert all(len(s.text) > 2 for s in r.segs if s.kind == "en")


def test_typo_recovered_in_top5():
    assert "リョウカイ！アリガトウ。" in inputs(top("ryoukai!arigatpu.")[:5])
    assert "コンニチハ" in inputs(top("konnnihciha")[:5])  # 入れ替え
    assert "キッテ" in inputs(top("kite")[:5]) or "キッテ" in inputs(top("kite"))  # 促音の脱字


def test_edit_limits_and_unedited_path_kept():
    rs = top("kyouhaosokunarisoudalara,sakinitabetwoite.", 20, max_edits=1)
    assert all(len(r.edits) <= 1 for r in rs)
    assert any(r.edits == () for r in rs)
    assert all(r.edits == () for r in top("kyouhaosokunari", 20, max_edits=0))


def test_order_coverage_and_breakdown():
    keys = "apiki-hakankyouhensuukarayomikomu."
    rs = top(keys)
    costs = [r.cost for r in rs]
    # 1位はコスト最小。上位5件とその後ろは、それぞれの中でコスト順（上位5件は直し方の違う候補を広く選ぶ）
    assert costs[0] == min(costs)
    assert costs[:5] == sorted(costs[:5]) and costs[5:] == sorted(costs[5:])
    assert len({tuple((s.kind, s.text, s.end) for s in r.segs) for r in rs}) == len(rs)
    for r in rs:
        assert r.segs[0].start == 0 and r.segs[-1].end == len(keys)
        assert all(a.end == b.start for a, b in zip(r.segs, r.segs[1:]))
        assert abs(sum(r.breakdown.values()) - r.cost) < 0.01


def test_same_reading_keeps_cheapest_path():
    # ジッケンノケッカは nn の揺れ（編集なし）でも脱字 n（編集あり）でも作れる。安いほうのコストと内訳を返す
    alt = next(r for r in top("jikkennokekka") if r.zenz_input() == "ジッケンノケッカ")
    assert alt.edits == () and alt.cost < 1.0


def test_top5_keeps_unedited_and_first_is_cheapest():
    # 探索で編集を安く数えても、1位は元のコストの最小で、編集なしの最良も上位5に残る
    for keys in ("dyi", "noiraiakuuyouni", "kyouhaosokunarisoudalara,sakinitabetwoite."):
        rs = top(keys)
        assert rs[0].cost == min(r.cost for r in rs)
        assert any(r.edits == () for r in rs[:5])


@pytest.mark.parametrize("keys, reading, token", [
    ("jrnoekimadearuku", "jrノエキマデアルク", "jr"),
    ("githubnipushshitejrnoapiwotamesu", "githubニpushシテjrノapiヲタメス", "jr"),
    ("cdmanokikakuwoshiraberu", "cdmaノキカクヲシラベル", "cdma"),
    ("scc-0210womitsukete", "scc-0210ヲミツケテ", "scc"),
    ("int8deryoushikasuru", "int8デリョウシカスル", "int8"),
    ("bf16tofp16nochigai", "bf16トfp16ノチガイ", "fp16"),  # 英字列の間の助詞（to）で切る
])
def test_raw_token_kept_in_top5(keys, reading, token):
    rs = top(keys)
    assert reading in inputs(rs[:5])
    r = next(r for r in rs if r.zenz_input() == reading)
    # 辞書に無い英字列は綴りのまま1区間
    assert any(s.kind == "en" and s.text == token and s.surfaces == (token,) for s in r.segs)


def test_raw_token_not_for_clean_romaji():
    # 正しいローマ字は英字のまま残さない（kai はカイ）
    for keys in ("kaigininaru", "sonotokinosoto", "kyouhaiitenkidesune"):
        r = top(keys, 5)[0]
        assert all(s.kind == "kana" for s in r.segs) and r.edits == ()


def test_sokuon_typo_arcs():
    assert "シュクショトシテツクッタシロ" in inputs(top("shukushotoshitetsuku6tashiro")[:5])  # t→6
    assert "タイトルロゴヲイッシン" in inputs(top("taitorurogowoiesshinn")[:5])  # 促音の前に余計な e


def test_gc_restored():
    import gc

    assert gc.isenabled()
    top("kyouhaiitenkidesune")
    assert gc.isenabled()
    gc.disable()
    try:
        top("kyouhaiitenkidesune")
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_every_table_syllable_parses_alone():
    for key, kana in TABLE.items():
        if key[0] in "xl" or key == "xn":
            continue
        assert kana in inputs(top(key, 5)), key


def test_kana_lm_context_after_english():
    from core.kana_lm import AFTER_EN, BOS, ORDER, load

    lm = load()
    # 英字の語の直後は助詞が来やすい（文頭よりノ・ヲの確率が高い）
    for c in "ノヲ":
        assert lm.logp(BOS * (ORDER - 2) + AFTER_EN, c) > lm.logp(BOS * (ORDER - 1), c)


def test_kana_lm_marked_runs_do_not_change_plain_contexts():
    import math

    from core.kana_lm import AFTER_EN, BOS, ORDER, train

    plain = train({"アイウ": 1.0, "ノアイ": 1.0})
    marked = train({"アイウ": 1.0, "ノアイ": 1.0, AFTER_EN + "ノア": 1.0})
    for hist, c in [(BOS * (ORDER - 1), "ア"), ("アイ", "ウ"), ("ノア", "イ")]:
        assert math.isclose(plain.logp(hist, c), marked.logp(hist, c))
    assert marked.logp(BOS * (ORDER - 2) + AFTER_EN, "ノ") > plain.logp(BOS * (ORDER - 2) + AFTER_EN, "ノ")


def test_kana_lm_is_normalized():
    import math

    from core.kana_lm import BOS, EOS, ORDER, train

    lm = train({"アイウ": 2.0, "アイエ": 1.0, "ウエオ": 0.5})
    chars = ["ア", "イ", "ウ", "エ", "オ", EOS]
    for hist in [BOS * (ORDER - 1), BOS + "アイ", "アイ", "ウエ", "オオオ"]:
        total = sum(math.exp(lm.logp(hist, c)) for c in chars)
        # 語彙外の1字分（1/V の重み）だけ 1 に届かない
        assert 0.85 < total <= 1.0 + 1e-9
    assert lm.logp("アイ", "ウ") > lm.logp("アイ", "エ") > lm.logp("アイ", "オ")
