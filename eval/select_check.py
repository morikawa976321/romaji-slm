"""工程3＋4 を、正解の読み（AJIMEE のカタカナ）から測る。読みの段（工程2）の誤りを除いた上限を見る。

    uv run python -m eval.select_check [--n 20] [--device cuda] [--w-dict 0 0.5 1]

辞書だけの1位・oracle@N・zenz で選んだ1位を並べる。w_dict ごとに zenz 選択の1位正解率を出す（重みの当たりを付ける）。
"""

import argparse
import statistics
import sys
import time

from core.kanakanji import Dictionary, convert, simple_reading
from core.select import ZenzScorer, rank

from .datasets import load_ajimee
from .metrics import loose_normalize


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--w-dict", type=float, nargs="+", default=[0.0, 0.25, 0.5, 1.0])
    ap.add_argument("--context", action="store_true", help="AJIMEE の context_text を左文脈に渡す")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    dic = Dictionary.load()
    scorer = ZenzScorer(device=args.device)
    items = load_ajimee(use_context=args.context)[: args.limit]
    n_dict = n_oracle = 0
    hits = {w: 0 for w in args.w_dict}
    loose_hits = {w: 0 for w in args.w_dict}
    ms = []
    shown = 0
    for it in items:
        convs = convert(simple_reading(it.meta["kana"]), dic, n=args.n)
        surfaces = [c.surface for c in convs]
        n_dict += bool(surfaces) and surfaces[0] in it.refs
        n_oracle += any(s in it.refs for s in surfaces)
        t0 = time.perf_counter()
        lms = None
        for w in args.w_dict:
            ranked = rank(convs, scorer, it.left_context, w_dict=w) if lms is None else sorted(
                lms, key=lambda s: -(s.lm - w * s.dict_cost / 500))
            if lms is None:
                lms = ranked
                ms.append((time.perf_counter() - t0) * 1000)
            top = ranked[0].conversion.surface if ranked else ""
            hits[w] += top in it.refs
            loose_hits[w] += loose_normalize(top) in {loose_normalize(r) for r in it.refs}
            if w == args.w_dict[0] and top not in it.refs and shown < args.show and any(s in it.refs for s in surfaces):
                shown += 1
                print(f"- {it.meta['kana'][:40]}\n  正解: {it.refs[0][:40]}\n  zenz: {top[:40]}\n  辞書1位: {surfaces[0][:40]}")
    n = len(items)
    print(f"件数 {n}、N={args.n}、左文脈 {'あり' if args.context else 'なし'}")
    print(f"辞書だけの1位 {n_dict / n:.1%}、oracle@{args.n} {n_oracle / n:.1%}")
    for w in args.w_dict:
        print(f"zenz で選ぶ（w_dict={w}）: 1位 {hits[w] / n:.1%}（記号・長音を無視 {loose_hits[w] / n:.1%}）")
    print(f"zenz 採点の時間 中央値 {statistics.median(ms):.0f} ms、最大 {max(ms):.0f} ms（{scorer.device}）")


if __name__ == "__main__":
    main()
