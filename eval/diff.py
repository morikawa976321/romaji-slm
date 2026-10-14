"""2つの結果 JSONL（eval/run.py が書く。同じセット）の差分を、1件単位の改善・悪化で見る。

    uv run python -m eval.diff eval/out/<before>__user_style.jsonl eval/out/<after>__user_style.jsonl [--show 20] [--md <path>]

id で突き合わせ、改善（exact 0→1）・悪化（1→0）・変わらずの件数、正常入力（typo・noisy 以外）で新たに
壊れた件数、誤補正・英語保持・ms 中央値の変化をカテゴリ別に Markdown で出す。
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

from .report import _TYPO_CATS, _mean, _pct


def load_rows(path: Path | str) -> dict[str, dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return {(r := json.loads(l))["id"]: r for l in lines if l.strip()}


def classify(b: dict, a: dict) -> str:
    """improved / regressed / both_ok / both_ng_changed / both_ng_same"""
    if a["exact"] and not b["exact"]:
        return "improved"
    if b["exact"] and not a["exact"]:
        return "regressed"
    if a["exact"]:
        return "both_ok"
    return "both_ng_changed" if a["output"] != b["output"] else "both_ng_same"


def _median(values) -> float | None:
    vs = [v for v in values if v is not None]
    return statistics.median(vs) if vs else None


def _side(rows: list[dict]) -> dict:
    return {
        "exact": _mean(r["exact"] for r in rows),
        "clean_exact": _mean(r["exact"] for r in rows if r["category"] not in _TYPO_CATS),
        "miscorrect": _mean(r.get("edited") for r in rows if r["category"] not in _TYPO_CATS),
        "en_kept": _mean(r.get("en_kept") for r in rows),
        "ms_median": _median(r.get("ms") for r in rows),
    }


def compare(before: dict[str, dict], after: dict[str, dict]) -> dict:
    ids = [i for i in before if i in after]
    pairs = [(before[i], after[i], classify(before[i], after[i])) for i in ids]
    cats = sorted({a["category"] for _, a, _ in pairs})
    by_cat = []
    for c in [*cats, "ALL"]:
        ps = [p for p in pairs if c == "ALL" or p[1]["category"] == c]
        counts = {k: sum(p[2] == k for p in ps)
                  for k in ("improved", "regressed", "both_ok", "both_ng_changed", "both_ng_same")}
        by_cat.append({
            "category": c, "n": len(ps), **counts,
            "clean_regressed": sum(p[2] == "regressed" and p[1]["category"] not in _TYPO_CATS for p in ps),
            "before": _side([p[0] for p in ps]), "after": _side([p[1] for p in ps]),
        })
    return {
        "by_cat": by_cat, "pairs": pairs,
        "only_before": [i for i in before if i not in after],
        "only_after": [i for i in after if i not in before],
    }


def _arrow(b, a, fmt) -> str:
    return f"{fmt(b)} → {fmt(a)}"


def _ms(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}"


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render(res: dict, before_name: str, after_name: str, show: int = 20) -> str:
    total = res["by_cat"][-1]
    lines = [f"# 差分: {before_name} → {after_name}", ""]
    if res["only_before"] or res["only_after"]:
        lines += [f"- **警告**: 片方にしか無い id（before のみ {len(res['only_before'])} 件、after のみ "
                  f"{len(res['only_after'])} 件）は数えていない。", ""]
    lines += [
        f"- 突き合わせた件数: {total['n']}",
        f"- 改善 {total['improved']} / 悪化 {total['regressed']}"
        f"（うち**正常入力で新たに壊れた {total['clean_regressed']}**）"
        f" / 両方正解 {total['both_ok']} / 両方不正解（出力変化）{total['both_ng_changed']}"
        f" / 両方不正解（出力同じ）{total['both_ng_same']}",
        "",
        "| カテゴリ | 件数 | 完全一致 | 改善 | 悪化 | 両方正解 | 両方不正解（出力変化） | 両方不正解（同じ） "
        "| 正常入力の完全一致 | 誤補正 | 英語保持 | ms/件（中央値） |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in res["by_cat"]:
        b, a = c["before"], c["after"]
        lines.append(
            f"| {c['category']} | {c['n']} | {_arrow(b['exact'], a['exact'], _pct)} | {c['improved']} "
            f"| {c['regressed']} | {c['both_ok']} | {c['both_ng_changed']} | {c['both_ng_same']} "
            f"| {_arrow(b['clean_exact'], a['clean_exact'], _pct)} | {_arrow(b['miscorrect'], a['miscorrect'], _pct)} "
            f"| {_arrow(b['en_kept'], a['en_kept'], _pct)} | {_arrow(b['ms_median'], a['ms_median'], _ms)} |")
    for kind, title in (("regressed", "悪化"), ("improved", "改善")):
        ps = [p for p in res["pairs"] if p[2] == kind]
        if not ps:
            continue
        lines += ["", f"## {title}（{len(ps)} 件中 {min(show, len(ps))} 件）", "",
                  "| id | カテゴリ | romaji | 正解 | before | after |", "|---|---|---|---|---|---|"]
        for b, a, _ in ps[:show]:
            lines.append(f"| {a['id']} | {a['category']} | {_cell(a['romaji'])} | {_cell(a['refs'][0])} "
                         f"| {_cell(b['output'])} | {_cell(a['output'])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--show", type=int, default=20, help="改善・悪化それぞれの例の件数")
    ap.add_argument("--md", help="Markdown の保存先")
    args = ap.parse_args(argv)
    res = compare(load_rows(args.before), load_rows(args.after))
    if res["only_before"] or res["only_after"]:
        print(f"警告: 片方にしか無い id: before のみ {res['only_before'][:10]}、after のみ {res['only_after'][:10]}",
              file=sys.stderr)
    md = render(res, Path(args.before).stem, Path(args.after).stem, args.show)
    print(md)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
