"""制約付き生成（core.constrained）を、正解の読み（AJIMEE のカタカナ）から測る。

    uv run python -m eval.constrained_check [--device cuda] [--context]
"""

import argparse
import statistics
import sys
import time

from core.constrained import ConstrainedZenz
from core.kanakanji import Dictionary, simple_reading
from core.select import ZenzScorer

from .datasets import load_ajimee
from .metrics import loose_normalize


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--context", action="store_true")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    dic = Dictionary.load()
    cz = ConstrainedZenz(ZenzScorer(device=args.device))
    items = load_ajimee(use_context=args.context)[: args.limit]
    hit = loose = 0
    ms = []
    shown = 0
    for it in items:
        t0 = time.perf_counter()
        out = cz.decode(simple_reading(it.meta["kana"]), dic, it.left_context)
        ms.append((time.perf_counter() - t0) * 1000)
        hit += out in it.refs
        loose += loose_normalize(out) in {loose_normalize(r) for r in it.refs}
        if out not in it.refs and shown < args.show:
            shown += 1
            print(f"- {it.meta['kana'][:40]}\n  正解: {it.refs[0][:40]}\n  出力: {out[:40]}")
    n = len(items)
    print(f"件数 {n}、左文脈 {'あり' if args.context else 'なし'}")
    print(f"制約付き生成: 1位 {hit / n:.1%}（記号・長音を無視 {loose / n:.1%}）")
    print(f"時間 中央値 {statistics.median(ms):.0f} ms、最大 {max(ms):.0f} ms（{args.device}）")


if __name__ == "__main__":
    main()
