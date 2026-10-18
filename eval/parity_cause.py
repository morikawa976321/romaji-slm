"""torch 版（T0）と llama 版（従来 L0）の食い違いを、片方だけを変えた変種の結果から原因に振り分ける（docs/LLAMA_PARITY.md）。

    uv run python -m eval.parity_cause [--md <path>]

使う変種（eval/out/parity/<tag>__<set>.jsonl。docs/LLAMA_PARITY.md「変種」）:
  T0 torch 既定 / T1 torch に行き止まりの刈り込み / L0 llama 従来（Q8_0・-np 5）/ L1 L0 の EOS を直したもの /
  L3 L1 を F16・-np 1 にしたもの（数値を torch に近づけ、実行ごとのぶれを無くす）

振り分け（最初に当てはまったもの）:
  EOS: L1 で T0 と同じ出力になる
  数値: L1 では残り、L3 で T0 と同じ出力になる（Q8_0 の量子化と -np 5 のバッチの計算の差）
  刈り込み: L3 でも残り、T1（torch に刈り込みを入れたもの）と L3 が同じ出力になる
  不明: どれでも消えない
"""

import argparse
import sys

from .parity import _TYPO_CATS, _c, _set_files, load

SETS = ("user_style", "gen_test", "ajimee", "wac_test", "file_eval_sets_typo_hard.jsonl",
        "file_eval_sets_user_style_nopunct.jsonl", "file_eval_sets_synth_user.jsonl")


def gap(r: dict) -> float | None:
    fin = sorted([c["score"] for c in r["cands"] if c["finished"]], reverse=True)
    return fin[0] - fin[1] if len(fin) >= 2 else None


def cause_rows(base_b: str = "L0") -> list[dict]:
    rows = []
    for s in SETS:
        get = {t: (load(_set_files(t)[s]) if s in _set_files(t) else {}) for t in ("T0", "T1", base_b, "L1", "L3")}
        T0, B = get["T0"], get[base_b]
        for i, a in T0.items():
            b = B.get(i)
            if b is None or a["output"] == b["output"]:
                continue

            def same(x, y):
                rx, ry = get[x].get(i), get[y].get(i)
                return None if rx is None or ry is None else rx["output"] == ry["output"]

            if same("T0", "L1"):
                cause = "EOS"
            elif same("T0", "L3"):
                cause = "数値"
            elif same("T1", "L3"):
                cause = "刈り込み"
            elif same("T0", "L3") is None:
                cause = "（L3 未計測）"
            else:
                cause = "不明"
            rows.append({"set": s, "id": i, "category": a["category"], "ref": a["refs"][0] if a["refs"] else "",
                         "t0": a["output"], "b": b["output"], "ok_t0": a.get("exact"), "ok_b": b.get("exact"),
                         "clean": a["category"] not in _TYPO_CATS, "gap": gap(a), "cause": cause})
    return rows


def render(rows: list[dict], base_b: str) -> str:
    from collections import Counter
    mk = lambda v: "" if v is None else ("○" if v else "×")  # noqa: E731
    lines = [f"原因の内訳: {', '.join(f'{k} {v}' for k, v in Counter(r['cause'] for r in rows).most_common())}（{len(rows)} 件）", "",
             f"| セット | id | カテゴリ | 正解 | torch（T0） | llama（{base_b}） | torch の 1位と2位の点差 | 原因 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        g = "—" if r["gap"] is None else f"{r['gap']:.2f}"
        lines.append(f"| {r['set'].replace('file_eval_sets_', '').replace('.jsonl', '')} | {r['id']} | {r['category']} "
                     f"| {_c(r['ref'])} | {_c(r['t0'])} {mk(r['ok_t0'])} | {_c(r['b'])} {mk(r['ok_b'])} | {g} | {r['cause']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="L0", help="振り分ける llama 版の変種（既定は従来の L0）")
    ap.add_argument("--md")
    args = ap.parse_args(argv)
    md = render(cause_rows(args.base), args.base)
    print(md)
    if args.md:
        from pathlib import Path
        Path(args.md).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
