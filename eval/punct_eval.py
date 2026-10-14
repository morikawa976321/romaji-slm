"""句読点の自動挿入（句読点 B）の評価。入力に句読点が無い件（gen_* の omit_punct）の結果 JSONL を読む。

    uv run python -m eval.punct_eval eval/out/<converter>__gen_val.jsonl [--category omit_punct] [--show 10]

- 本文の保持: 出力と正解から句読点（、。？！?!,.）だけを除いて一致した割合。
- 句読点の適合率・再現率・F1（種類別）: 位置は「句読点を除いた本文の何文字目の後ろか」。本文が一致した件だけで数える。
- 不要な挿入率: 陰性（正解に 、。？? が無い件）のうち、出力に句読点が入った割合。
- 文末の句点の再現率と文中の読点の再現率を分けて出す。
"""

import argparse
import sys

from .diff import _cell, load_rows
from .metrics import PUNCT, strip_punct

KIND = {"、": "読点", ",": "読点", "。": "句点", ".": "句点", "？": "疑問符", "?": "疑問符",
        "！": "感嘆符", "!": "感嘆符"}
KINDS = ("読点", "句点", "疑問符", "感嘆符")
# 陽性・陰性を分ける記号（依頼の定義どおり感嘆符は含めない）
_POSITIVE_MARKS = frozenset("、。？?")


def marks(s: str) -> tuple[str, set[tuple[int, str]]]:
    """(句読点を除いた本文, {(本文の何文字目の後ろか, 種類)})"""
    body, out = [], set()
    for ch in s:
        if ch in PUNCT:
            out.add((len(body), KIND[ch]))
        else:
            body.append(ch)
    return "".join(body), out


def _counts(ref: set, hyp: set) -> dict[str, list[int]]:
    """種類ごとの [TP, FP, FN]"""
    c = {k: [0, 0, 0] for k in KINDS}
    for m in hyp & ref:
        c[m[1]][0] += 1
    for m in hyp - ref:
        c[m[1]][1] += 1
    for m in ref - hyp:
        c[m[1]][2] += 1
    return c


def evaluate(rows: list[dict]) -> dict:
    res = {"n": len(rows), "body_ok": 0, "pos": 0, "neg": 0, "neg_inserted": 0,
           "tp_fp_fn": {k: [0, 0, 0] for k in KINDS},
           "final_period": [0, 0], "mid_comma": [0, 0],  # [当たり, 正解の数]
           "body_mismatch": [], "over": [], "under": [], "misplaced": []}
    for r in rows:
        positive = any(ch in _POSITIVE_MARKS for ch in r["refs"][0])
        res["pos" if positive else "neg"] += 1
        hyp_body, hyp = marks(r["output"])
        if not positive and hyp:
            res["neg_inserted"] += 1
        # 本文が一致する正解のうち、句読点の食い違いが最も少ないもの
        cands = [(len(hyp ^ m), b, m) for b, m in map(marks, r["refs"]) if b == hyp_body]
        if not cands:
            res["body_mismatch"].append(r)
            continue
        res["body_ok"] += 1
        _, body, ref = min(cands, key=lambda t: t[0])
        for k, v in _counts(ref, hyp).items():
            for j in range(3):
                res["tp_fp_fn"][k][j] += v[j]
        for key, want in (("final_period", {m for m in ref if m == (len(body), "句点")}),
                          ("mid_comma", {m for m in ref if m[1] == "読点" and m[0] < len(body)})):
            res[key][0] += len(want & hyp)
            res[key][1] += len(want)
        fp, fn = hyp - ref, ref - hyp
        if fp and fn:
            res["misplaced"].append(r)
        elif fp:
            res["over"].append(r)
        elif fn:
            res["under"].append(r)
    return res


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def prf(tp: int, fp: int, fn: int) -> tuple[float | None, float | None, float | None]:
    p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    if p is None or r is None:
        return p, r, None
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def render(res: dict, name: str, show: int = 10) -> str:
    lines = [
        f"# 句読点の評価: {name}", "",
        f"- 件数 {res['n']}（陽性 {res['pos']} / 陰性 {res['neg']}）",
        f"- 本文の保持: {_pct(_ratio(res['body_ok'], res['n']))}（{res['body_ok']}/{res['n']}）。"
        f"本文が違う {len(res['body_mismatch'])} 件は句読点の集計から外す",
        f"- 不要な挿入率（陰性のうち出力に句読点）: {_pct(_ratio(res['neg_inserted'], res['neg']))}"
        f"（{res['neg_inserted']}/{res['neg']}）",
        f"- 文末の句点の再現率: {_pct(_ratio(*res['final_period']))}（{res['final_period'][0]}/{res['final_period'][1]}）",
        f"- 文中の読点の再現率: {_pct(_ratio(*res['mid_comma']))}（{res['mid_comma'][0]}/{res['mid_comma'][1]}）",
        "", "| 種類 | 正解の数 | 出力の数 | 適合率 | 再現率 | F1 |", "|---|---|---|---|---|---|",
    ]
    tot = [0, 0, 0]
    for k in KINDS:
        tp, fp, fn = res["tp_fp_fn"][k]
        tot = [tot[0] + tp, tot[1] + fp, tot[2] + fn]
        p, r, f = prf(tp, fp, fn)
        lines.append(f"| {k} | {tp + fn} | {tp + fp} | {_pct(p)} | {_pct(r)} | {_pct(f)} |")
    p, r, f = prf(*tot)
    lines.append(f"| 全体 | {tot[0] + tot[2]} | {tot[0] + tot[1]} | {_pct(p)} | {_pct(r)} | {_pct(f)} |")
    for key, title in (("over", "挿入しすぎ"), ("under", "足りない"), ("misplaced", "位置違い"),
                       ("body_mismatch", "本文が違う")):
        rs = res[key]
        if not rs:
            continue
        lines += ["", f"## {title}（{len(rs)} 件中 {min(show, len(rs))} 件）", "",
                  "| id | romaji | 正解 | 出力 |", "|---|---|---|---|"]
        for r in rs[:show]:
            lines.append(f"| {r['id']} | {_cell(r['romaji'])} | {_cell(r['refs'][0])} | {_cell(r['output'])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("--category", default="omit_punct", help="対象のカテゴリ（all で全件）")
    ap.add_argument("--show", type=int, default=10, help="失敗の種類ごとの例の件数")
    args = ap.parse_args(argv)
    rows = [r for r in load_rows(args.results).values() if args.category == "all" or r["category"] == args.category]
    print(render(evaluate(rows), f"{args.results}（{args.category}）", args.show))


if __name__ == "__main__":
    main()
