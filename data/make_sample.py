"""生成物の train/valid/test から決定的に N 件を選び、1つの JSONL にする（レビュー用の見本）。

    uv run python -m data.make_sample data/out/ data/samples/sample_200.jsonl --n 200 --seed 0
"""

import argparse
import random
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    lines = [ln for k in ("train", "valid", "test") for ln in (Path(a.src) / f"{k}.jsonl").read_text(encoding="utf-8").splitlines()]
    random.Random(a.seed).shuffle(lines)
    Path(a.dst).write_text("\n".join(lines[: a.n]) + "\n", encoding="utf-8")
    print(f"{min(a.n, len(lines))} 件 → {a.dst}")


if __name__ == "__main__":
    main()
