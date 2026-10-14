import json

from eval import datasets
from eval.diff import compare, load_rows, main, render
from eval.report import aggregate, render_markdown


def _row(id, cat, output, ref, *, edited=None, en_kept=None, ms=100.0):
    return {"id": id, "category": cat, "romaji": f"r{id}", "refs": [ref], "output": output,
            "exact": output == ref, "loose": output == ref, "min_cer": 0.0, "edited": edited,
            "en_kept": en_kept, "ms": ms, "n_gen_tokens": 1}


BEFORE = [
    _row("0", "chat", "今日は", "今日は", edited=False, ms=100),       # 両方正解
    _row("1", "chat", "明日", "明日", edited=False, ms=200),           # 悪化（正常入力）
    _row("2", "tech", "ぎっと", "git", edited=False, en_kept=0.0, ms=300),  # 改善
    _row("3", "typo", "ありがtぷ", "ありがとう", edited=False, ms=400),  # 改善
    _row("4", "typo", "い", "い", edited=True, ms=500),                 # 悪化だが打鍵ミス
    _row("5", "memo", "x", "y", edited=False, ms=600),                 # 両方不正解・出力同じ
    _row("6", "memo", "p", "q", edited=False, ms=700),                 # 両方不正解・出力変化
    _row("7", "memo", "z", "z", edited=False, ms=800),                 # before のみ
]
AFTER = [
    _row("0", "chat", "今日は", "今日は", edited=False, ms=50),
    _row("1", "chat", "あした", "明日", edited=True, ms=60),
    _row("2", "tech", "git", "git", edited=False, en_kept=1.0, ms=70),
    _row("3", "typo", "ありがとう", "ありがとう", edited=True, ms=80),
    _row("4", "typo", "う", "い", edited=True, ms=90),
    _row("5", "memo", "x", "y", edited=False, ms=100),
    _row("6", "memo", "pp", "q", edited=False, ms=110),
    _row("8", "memo", "w", "w", edited=False, ms=120),                 # after のみ
]


def test_compare_counts():
    res = compare({r["id"]: r for r in BEFORE}, {r["id"]: r for r in AFTER})
    assert res["only_before"] == ["7"] and res["only_after"] == ["8"]
    by = {c["category"]: c for c in res["by_cat"]}
    t = by["ALL"]
    assert t["n"] == 7
    assert (t["improved"], t["regressed"], t["both_ok"], t["both_ng_changed"], t["both_ng_same"]) == (2, 2, 1, 1, 1)
    assert t["clean_regressed"] == 1  # id 1 だけ（id 4 は typo）
    assert by["typo"]["regressed"] == 1 and by["typo"]["clean_regressed"] == 0
    # 正常入力（typo 以外の 5 件）: before 正解 0,1 → 2/5、after 正解 0,2 → 2/5
    assert t["before"]["clean_exact"] == 2 / 5 and t["after"]["clean_exact"] == 2 / 5
    # 誤補正は正常入力 5 件のうち edited: before 0、after 1（id 1）
    assert t["before"]["miscorrect"] == 0 and t["after"]["miscorrect"] == 1 / 5
    assert (t["before"]["en_kept"], t["after"]["en_kept"]) == (0.0, 1.0)
    assert t["before"]["ms_median"] == 400 and t["after"]["ms_median"] == 80
    assert t["before"]["exact"] == 3 / 7 and t["after"]["exact"] == 3 / 7


def test_render_and_main(tmp_path, capsys):
    b, a = tmp_path / "b.jsonl", tmp_path / "a.jsonl"
    for p, rows in ((b, BEFORE), (a, AFTER)):
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    assert set(load_rows(b)) == {r["id"] for r in BEFORE}
    md_path = tmp_path / "d.md"
    main([str(b), str(a), "--show", "1", "--md", str(md_path)])
    out = capsys.readouterr()
    assert "警告" in out.err and "警告" in out.out
    assert "正常入力で新たに壊れた 1" in out.out
    assert "| ALL | 7 | 42.9% → 42.9% | 2 | 2 | 1 | 1 | 1 |" in out.out
    assert "## 悪化（2 件中 1 件）" in out.out and "## 改善（2 件中 1 件）" in out.out
    assert md_path.read_text(encoding="utf-8") == out.out.rstrip("\n") + "\n"
    # 区切り文字を含む出力でも表が崩れない
    assert "a\\|b" in render(compare({"0": _row("0", "c", "a|b", "x")}, {"0": _row("0", "c", "x", "x")}), "b", "a")


def test_clean_exact_and_pair_consistent():
    rows = [
        _row("0", "chat", "今日は", "今日は"),
        _row("1", "chat", "明日", "明後日"),
        _row("2", "typo", "今日は", "今日は") | {"pair_of": "0"},   # 正常版と同じ出力
        _row("3", "typo", "あした", "明後日") | {"pair_of": "1"},   # 違う出力
        _row("4", "typo", "x", "x"),                                # 対なし
        _row("5", "noisy", "y", "z"),
    ]
    agg = {a["category"]: a for a in aggregate(rows)}
    assert agg["ALL"]["clean_exact"] == 1 / 2
    assert agg["typo"]["clean_exact"] is None
    assert agg["ALL"]["pair_consistent"] == 1 / 2 and agg["typo"]["pair_consistent"] == 1 / 2
    assert agg["chat"]["pair_consistent"] is None
    # 行に pair_of が無くても、対応表を渡せば数える（eval/run.py の行には meta が無い）
    plain = [{k: v for k, v in r.items() if k != "pair_of"} for r in rows]
    assert aggregate(plain)[-1]["pair_consistent"] is None
    assert aggregate(plain, {"2": "0", "3": "1"})[-1]["pair_consistent"] == 1 / 2
    md = render_markdown("x", "s", rows, {})
    assert "| ALL | 6 |" in md and md.rstrip().endswith("| 50.0% | 50.0% |")


def test_user_style_pairs():
    items = datasets.load_set("user_style")
    by_id = {i.id: i for i in items}
    pairs = {i.id: i.pair_of for i in items if i.pair_of}
    assert len(pairs) == 9
    for t, c in pairs.items():
        assert by_id[t].category == "typo" and by_id[c].category not in ("typo", "noisy")
        assert by_id[t].refs[0] == by_id[c].refs[0]


def test_gen_dev_val_split():
    full = datasets.load_set("gen_test")
    dev, val = datasets.load_set("gen_dev"), datasets.load_set("gen_val")
    assert len(dev) + len(val) == len(full) == 337
    key = lambda i: (i.romaji, i.refs[0], i.left_context)
    assert sorted(map(key, dev + val)) == sorted(map(key, full))
    assert not {i.romaji for i in dev} & {i.romaji for i in val}
    for cat in ("clean", "noisy", "omit_punct"):
        d = sum(i.category == cat for i in dev)
        v = sum(i.category == cat for i in val)
        assert abs(d - v) <= 2
