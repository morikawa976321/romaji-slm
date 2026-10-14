"""gen_test.jsonl を dev（閾値・重みの調整用）と val（最終確認用）に決定的に分ける。

    uv run python -m eval.split_gen

「カテゴリ × 正解に句読点（、。？?）があるか」の層ごとに、id（無ければ romaji）の SHA-256 の順に並べて
前半を dev、後半を val にする。カテゴリの比率と、句読点評価（eval/punct_eval.py）の陽性・陰性の比率が
両方でほぼ同じになる。同じ romaji の行は同じ側に入る。元の gen_test.jsonl は残す。
"""

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from .datasets import SETS


def _key(d: dict) -> str:
    k = d.get("id") or (d.get("meta") or {}).get("id") or d["romaji"]
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    by_cat = defaultdict(list)
    for i, d in enumerate(rows):
        cat = (d.get("meta") or {}).get("category", "all")
        by_cat[cat, any(c in d["output"] for c in "、。？?")].append((_key(d), i))
    dev_idx = set()
    for keys in by_cat.values():
        keys.sort()
        cut = (len(keys) + 1) // 2
        # 同じキー（重複した romaji）が境目をまたがないように境目を後ろへずらす
        while 0 < cut < len(keys) and keys[cut][0] == keys[cut - 1][0]:
            cut += 1
        dev_idx.update(i for _, i in keys[:cut])
    # 元の順を保つ
    return ([d for i, d in enumerate(rows) if i in dev_idx],
            [d for i, d in enumerate(rows) if i not in dev_idx])


def main(src: Path = SETS / "gen_test.jsonl", out_dir: Path = SETS):
    lines = [l for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines]
    dev, val = split(rows)
    for name, part in (("gen_dev", dev), ("gen_val", val)):
        (out_dir / f"{name}.jsonl").write_text(
            "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in part), encoding="utf-8")
        c = Counter((d.get("meta") or {}).get("category", "all") for d in part)
        print(name, len(part), " ".join(f"{k}={v}（{v / len(part):.1%}）" for k, v in sorted(c.items())))


if __name__ == "__main__":
    main()
