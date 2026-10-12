"""工程3の合格条件: AJIMEE の正解読みを core.kanakanji に与え、1位正解率と oracle@N を測る（docs/KANAKANJI.md）。

    uv run python -m eval.kanakanji_check [--n 20] [--show 10] [--set unk_kata_per_char=2000 ...]

入力は meta["kana"]（正解のカタカナ読み）を simple_reading で区間に切ったもの（かなは1区間、記号は sym 区間）。
"""

import argparse
import dataclasses
import json
import os
import resource
import statistics
import sys
import time

from core.kanakanji import MOZC_DIR, Costs, Dictionary, convert, simple_reading

from .datasets import load_ajimee
from .metrics import loose_normalize


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return float("nan")


def run(dic: Dictionary, items, n: int) -> dict:
    rows = []
    times = []
    for it in items:
        reading = simple_reading(it.meta["kana"])
        t0 = time.perf_counter()
        convs = convert(reading, dic, n)
        times.append((time.perf_counter() - t0) * 1000)
        hyps = [c.surface for c in convs]
        refs = set(it.refs)
        lrefs = {loose_normalize(r) for r in it.refs}
        rank = next((k for k, h in enumerate(hyps) if h in refs), None)
        lrank = next((k for k, h in enumerate(hyps) if loose_normalize(h) in lrefs), None)
        rows.append({"id": it.id, "kana": it.meta["kana"], "refs": it.refs, "top": hyps[0] if hyps else "",
                     "rank": rank, "loose_rank": lrank, "top_cost": convs[0].cost if convs else None,
                     "top_words": [f"{w.surface}/{w.reading}/{w.source}" for w in convs[0].words] if convs else []})
    k = len(rows)
    return {
        "n": k,
        "top1": sum(r["rank"] == 0 for r in rows) / k,
        "oracle": sum(r["rank"] is not None for r in rows) / k,
        "top1_loose": sum(r["loose_rank"] == 0 for r in rows) / k,
        "oracle_loose": sum(r["loose_rank"] is not None for r in rows) / k,
        "ms_median": statistics.median(times),
        "ms_p95": sorted(times)[int(0.95 * (k - 1))],
        "ms_max": max(times),
        "rows": rows,
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--show", type=int, default=10, help="失敗例（1位が正解でない項目）を何件表示するか")
    ap.add_argument("--set", nargs="*", default=[], metavar="NAME=VALUE", help="Costs の値を上書きする")
    ap.add_argument("--dict", default=str(MOZC_DIR))
    ap.add_argument("--json", help="項目ごとの結果を書き出す")
    args = ap.parse_args()

    costs = Costs()
    for kv in args.set:
        name, value = kv.split("=", 1)
        if name not in {f.name for f in dataclasses.fields(Costs)}:
            sys.exit(f"Costs に無い名前: {name}")
        setattr(costs, name, int(value))

    rss0 = _rss_mb()
    dic = Dictionary.load(args.dict, costs)
    rss1 = _rss_mb()
    items = load_ajimee(use_context=False)[: args.limit]
    convert(simple_reading("ウォームアップ"), dic, args.n)
    res = run(dic, items, args.n)
    rss2 = _rss_mb()

    print(f"辞書の読み込み: {dic.stats['load_sec']:.2f} 秒（{dic.stats['source']}）、"
          f"RSS {rss0:.0f} → {rss1:.0f} MB（読み込み後）→ {rss2:.0f} MB（評価後）、"
          f"ru_maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
    print(f"Costs: {dataclasses.asdict(costs)}")
    print(f"件数 {res['n']}、N={args.n}、CPU {os.cpu_count()} 論理コア")
    print(f"1位正解 {res['top1']:.1%}（記号・長音を無視 {res['top1_loose']:.1%}）")
    print(f"oracle@{args.n} {res['oracle']:.1%}（記号・長音を無視 {res['oracle_loose']:.1%}）")
    print(f"変換時間 中央値 {res['ms_median']:.1f} ms、p95 {res['ms_p95']:.1f} ms、最大 {res['ms_max']:.1f} ms")
    fails = [r for r in res["rows"] if r["rank"] != 0]
    for r in fails[: args.show]:
        print(f"- [{r['id']}] {r['kana']}\n    正解: {' / '.join(r['refs'])}\n    1位: {r['top']}"
              f"（正解の順位 {'圏外' if r['rank'] is None else r['rank'] + 1}）\n    語: {' '.join(r['top_words'])}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
