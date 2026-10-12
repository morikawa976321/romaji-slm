from pathlib import Path

import pytest

from core import kanakanji
from core.kanakanji import MOZC_DIR, Dictionary, convert, hira_to_kata, simple_reading
from core.types import Reading, Seg

POS = [
    "BOS/EOS,*,*,*,*,*,*",
    "名詞,一般,*,*,*,*,*",
    "助詞,係助詞,*,*,*,*,は",
    "名詞,数,アラビア数字,*,*,*,*",
    "記号,句点,*,*,*,*,*",
    "記号,読点,*,*,*,*,*",
]
NOUN, WA, NUM, KUTEN, TOUTEN = 1, 2, 3, 4, 5


def _matrix():
    m = [[1000] * len(POS) for _ in POS]
    m[0][0] = 0
    m[NOUN][WA] = 100  # 名詞→は は安い
    m[WA][NOUN] = 200
    return m


ENTRIES = [
    ("きょう", NOUN, NOUN, 3000, "今日"),
    ("きょう", NOUN, NOUN, 4000, "京"),
    ("は", WA, WA, 500, "は"),
    ("てんき", NOUN, NOUN, 3000, "天気"),
    ("てんき", NOUN, NOUN, 3500, "転機"),
    ("てん", NOUN, NOUN, 3000, "点"),
    ("き", NOUN, NOUN, 3000, "木"),
]


@pytest.fixture
def dic():
    return Dictionary.from_entries(ENTRIES, _matrix(), POS)


def _recompute(conv, dic):
    """語のコストと接続コスト（BOS/EOS を含む）を足し直す。"""
    total, prev = 0, 0
    for w in conv.words:
        total += dic.conn(prev, w.lid) + w.cost
        prev = w.rid
    return total + dic.conn(prev, 0)


def test_best_path(dic):
    convs = convert(simple_reading("キョウハテンキ"), dic, 5)
    assert convs[0].surface == "今日は天気"
    assert [w.source for w in convs[0].words] == ["dict", "dict", "dict"]
    assert [w.reading for w in convs[0].words] == ["キョウ", "ハ", "テンキ"]


def test_nbest_sorted_unique_and_costs_add_up(dic):
    convs = convert(simple_reading("キョウハテンキ"), dic, 20)
    surfaces = [c.surface for c in convs]
    assert len(surfaces) == len(set(surfaces)) == 20
    costs = [c.cost for c in convs]
    assert costs == sorted(costs)
    assert {"京は天気", "今日は転機", "今日は点木"} <= set(surfaces)
    for c in convs:
        assert c.cost == _recompute(c, dic)


def test_words_explain_the_reading(dic):
    """表層と読みの対応が取れない語を作らない: 読みをつなぐと区間の読みに戻り、辞書の語は辞書にある組。"""
    entries = {(hira_to_kata(r), s) for r, _, _, _, s in ENTRIES}
    reading = simple_reading("キョウハテンキ")
    for c in convert(reading, dic, 50):
        assert "".join(w.reading for w in c.words) == reading.segs[0].text
        for w in c.words:
            if w.source == "dict":
                assert (w.reading, w.surface) in entries
            else:
                assert w.source == "kana"
                assert w.surface in (w.reading, kanakanji.kata_to_hira(w.reading))


def test_unknown_kana_never_dead_ends(dic):
    convs = convert(simple_reading("ズズキョウ"), dic, 5)
    assert convs, "辞書に無い読みでも候補が出る"
    best = convs[0]
    assert best.surface.endswith("今日")
    assert best.words[0].source == "kana" and best.words[0].lid == dic.unk_id
    assert any(c.surface == "ずず今日" for c in convs)


def test_mixed_segments(dic):
    reading = Reading(segs=(
        Seg("kana", 0, 5, "キョウハ"),
        Seg("en", 5, 11, "github", ("GitHub", "github")),
        Seg("num", 11, 13, "20"),
        Seg("sym", 13, 14, ".", ("。", ".")),
    ))
    convs = convert(reading, dic, 10)
    surfaces = [c.surface for c in convs]
    assert surfaces[0] == "今日はGitHub20。"
    assert {"今日はgithub20。", "今日はGitHub20."} <= set(surfaces)
    w = convs[0].words
    assert [x.source for x in w] == ["dict", "dict", "en", "num", "sym"]
    assert [x.seg_index for x in w] == [0, 0, 1, 2, 3]
    assert w[2].reading == "github" and w[2].lid == NOUN
    assert w[3].lid == NUM and w[4].lid == KUTEN
    for c in convs:
        assert c.cost == _recompute(c, dic)


def test_sym_pos(dic):
    assert dic.sym_id("、") == TOUTEN
    assert dic.sym_id("。") == KUTEN
    assert dic.sym_id("#") == NOUN  # 記号,一般 が無い表では名詞


def test_bos_eos_connection():
    m = _matrix()
    m[0][WA] = 9000  # 文頭の「は」は高い
    d = Dictionary.from_entries(ENTRIES + [("は", NOUN, NOUN, 2000, "葉")], m, POS)
    assert convert(simple_reading("ハ"), d, 1)[0].surface == "葉"
    assert convert(simple_reading("キョウハ"), d, 1)[0].surface == "今日は"


def test_simple_reading():
    r = simple_reading("きょうはgithubで20,")
    assert [(s.kind, s.text, s.surfaces) for s in r.segs] == [
        ("kana", "キョウハ", ()), ("en", "github", ()), ("kana", "デ", ()), ("num", "20", ()),
        ("sym", ",", ("、", ",")),
    ]


def test_dict_index_fallback():
    idx = kanakanji._DictIndex([("キ", b"a"), ("キョウ", b"b"), ("キョウ", b"c")])
    assert idx.prefixes("キョウハ") == ["キ", "キョウ"]
    assert idx["キョウ"] == [b"b", b"c"]


def _write_mozc(path: Path):
    path.mkdir()
    (path / "id.def").write_text("".join(f"{i} {p}\n" for i, p in enumerate(POS)), encoding="utf-8")
    m = _matrix()
    m[0][0] = 7  # Mozc は rid=lid=0 を 0 に読み替える
    flat = [m[r][l] for r in range(len(POS)) for l in range(len(POS))]
    (path / "connection_single_column.txt").write_text(f"{len(POS)}\n" + "".join(f"{c}\n" for c in flat))
    lines = [f"{r}\t{l}\t{ri}\t{c}\t{s}" for r, l, ri, c, s in ENTRIES]
    lines.append("きよう\t1\t1\t100\t今日\tSPELLING_CORRECTION")  # 誤った読みは採らない
    (path / "dictionary00.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("cache", [False, True])
def test_load_mozc_format(tmp_path, cache):
    _write_mozc(tmp_path / "mozc")
    for _ in range(2):  # cache=True なら2回目は compiled/ から
        d = Dictionary.load(tmp_path / "mozc", cache=cache)
        assert d.conn(0, 0) == 0
        assert d.conn(NOUN, WA) == 100 and d.conn(WA, NOUN) == 200
        assert sorted(d.lookup("キョウ")) == [(NOUN, NOUN, 3000, "今日"), (NOUN, NOUN, 4000, "京")]
        assert d.lookup("キヨウ") == []
        assert convert(simple_reading("キョウハテンキ"), d, 1)[0].surface == "今日は天気"
    if cache and kanakanji.marisa_trie is not None:
        assert d.stats["source"] == "cache"


@pytest.mark.skipif(not (MOZC_DIR / "compiled" / "STAMP").exists() and not (MOZC_DIR / "dictionary00.txt").exists(),
                    reason="Mozc 辞書が無い（uv run python -m core.fetch_mozc）")
def test_real_mozc():
    d = Dictionary.load()
    convs = convert(simple_reading("キョウハイイテンキデスネ"), d, 20)
    assert "今日はいい天気ですね" in [c.surface for c in convs[:3]]
    convs = convert(simple_reading("ギジュツテキナモンダイガアリマス"), d, 20)
    assert convs[0].surface == "技術的な問題があります"
