"""変換1件の時間を段階ごとに分ける（docs/SPEEDUP.md）。

    uv run python -m eval.parity_speed --converter "core-ort:t=4,p=insert" --device cpu
    uv run python -m eval.parity_speed --converter "core:p=insert" --device cuda

入力は user_style の正解 20〜45 字。段階:

- read: 読みの列挙と候補の絞り込み（core.reading / core.preselect。どの実行方式でも共通）
- decode: k 件の制約付き生成。forward と照合（_pick）に分ける
- fluency: 候補の完成文の自然さ
- punct: 句読点 B の採点（p=insert のときだけ）
- other: 残り（点数の計算・表記の描画など）

速度は測った機械と条件を添えて記録する（CLAUDE.md）。結果は eval/out/parity/speed-<日時>.json と .md。
"""

import argparse
import json
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .converters import make_converter
from .datasets import load_set
from .report import env_info

OUT = Path(__file__).resolve().parent / "out" / "parity"


class Clock:
    def __init__(self):
        self.t: dict[str, float] = {}
        self.n: dict[str, int] = {}
        self.stack: list[str] = []

    def add(self, k: str, ms: float, n: int = 1) -> None:
        self.t[k] = self.t.get(k, 0.0) + ms
        self.n[k] = self.n.get(k, 0) + n

    @contextmanager
    def span(self, k: str):
        t0 = time.perf_counter()
        self.stack.append(k)
        try:
            yield
        finally:
            self.stack.pop()
            self.add(k, (time.perf_counter() - t0) * 1000)

    def phase(self) -> str:
        return self.stack[-1] if self.stack else ""


class _Cur:
    """計時の包みが読む、いまの件の Clock（件ごとに差し替える）。"""

    def __init__(self):
        self.c = Clock()

    def __getattr__(self, k):
        return getattr(self.c, k)


def instrument(conv, clock: "_Cur") -> None:
    import core.pipeline as P
    import core.preselect as PS

    orig_readings, orig_pre = P.readings, PS.preselect

    def readings(*a, **k):
        with clock.span("read"):
            return orig_readings(*a, **k)

    def pre(*a, **k):
        with clock.span("read"):
            return orig_pre(*a, **k)

    P.readings, PS.preselect = readings, pre
    be = getattr(conv, "backend", None)
    target = be if be is not None else conv.cz
    orig_dec = target.decode_batch

    def decode_batch(*a, **k):
        with clock.span("decode"):
            return orig_dec(*a, **k)

    target.decode_batch = decode_batch
    orig_flu = conv._fluency

    def _fluency(*a, **k):
        with clock.span("fluency" if clock.phase() != "punct" else "punct_fluency"):
            return orig_flu(*a, **k)

    conv._fluency = _fluency
    orig_punct = conv.insert_punct

    def insert_punct(*a, **k):
        with clock.span("punct"):
            return orig_punct(*a, **k)

    conv.insert_punct = insert_punct
    if be is not None and hasattr(be, "_run"):
        # ONNX Runtime 版（core.ort_backend）: forward と照合を分ける
        orig_run, orig_bpick = be._run, be._pick

        def _run(*a, **k):
            t0 = time.perf_counter()
            r = orig_run(*a, **k)
            clock.add(f"{clock.phase()}.forward", (time.perf_counter() - t0) * 1000)
            return r

        def _bpick(*a, **k):
            t0 = time.perf_counter()
            r = orig_bpick(*a, **k)
            clock.add("decode.pick", (time.perf_counter() - t0) * 1000)
            return r

        be._run, be._pick = _run, _bpick
    if be is None:
        model = conv.scorer.model
        orig_fwd = model.forward

        def forward(*a, **k):
            t0 = time.perf_counter()
            r = orig_fwd(*a, **k)
            ph = clock.phase()
            clock.add(f"{ph}.forward", (time.perf_counter() - t0) * 1000)
            return r

        model.forward = forward
        cz = conv.cz
        orig_pick = cz._pick

        def _pick(*a, **k):
            t0 = time.perf_counter()
            r = orig_pick(*a, **k)
            clock.add("decode.pick", (time.perf_counter() - t0) * 1000)
            return r

        cz._pick = _pick


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--converter", required=True)
    ap.add_argument("--model")
    ap.add_argument("--device", default="cuda", help="記録に書く装置（core-ort は CPU で動く。torch 版 core は cuda だけ）")
    ap.add_argument("--set", default="user_style")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=45)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--repeat", type=int, default=1, help="入力の列を何周するか")
    ap.add_argument("--tag", default="")
    args = ap.parse_args(argv)
    items = [it for it in load_set(args.set) if args.min_chars <= len(it.refs[0]) <= args.max_chars]
    conv = make_converter(args.converter, args.device, args.model)
    env = env_info(args.device)
    try:
        for _ in range(args.warmup):
            conv.convert("konnichiha")
            for it in items[:3]:
                conv.convert(it.romaji, it.left_context)
        clock_all: list[dict] = []
        be = getattr(conv, "backend", None)
        cur = _Cur()
        instrument(conv, cur)
        for _ in range(args.repeat):
            for it in items:
                cur.c = Clock()
                t0 = time.perf_counter()
                conv.convert(it.romaji, it.left_context)
                total = (time.perf_counter() - t0) * 1000
                clock_all.append({"total": total, **cur.c.t, "n": cur.c.n, "chars": len(it.refs[0])})
        md = render(clock_all, env, args)
        print(md)
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"speed-{args.tag or 'run'}-{stamp}"
        (OUT / f"{name}.md").write_text(md, encoding="utf-8")
        (OUT / f"{name}.json").write_text(json.dumps({"env": env, "args": vars(args), "rows": clock_all},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        if hasattr(conv, "close"):
            conv.close()


def _med(xs):
    return statistics.median(xs) if xs else float("nan")


def render(rows: list[dict], env: dict, args) -> str:
    keys = ["total", "read", "decode", "decode.forward", "decode.pick", "fluency", "fluency.forward", "punct",
            "punct_fluency", "punct_fluency.forward"]
    lines = [f"# 速度の内訳: {args.converter}（{time.strftime('%Y-%m-%d %H:%M')}）", "",
             f"- 機械: {env.get('cpu')}（論理 {env.get('logical_cpus')}）、{args.device}、torch {env.get('torch', '-')}",
             f"- モデル: {args.model or 'HF Miwa-Keita/zenz-v3.1-small'}",
             f"- 入力: {args.set} の正解 {args.min_chars}〜{args.max_chars} 字、{len(rows)} 件（{args.repeat} 周）、空回し {args.warmup} 回",
             "", "| 段階 | 1件の中央値 ms | 平均 ms |", "|---|---|---|"]
    for k in keys:
        xs = [r.get(k, 0.0) for r in rows]
        if any(xs):
            lines.append(f"| {k} | {_med(xs):.1f} | {statistics.mean(xs):.1f} |")
    other = [r["total"] - r.get("read", 0) - r.get("decode", 0) - r.get("fluency", 0) - r.get("punct", 0) for r in rows]
    lines.append(f"| other | {_med(other):.1f} | {statistics.mean(other):.1f} |")
    tot = sorted(r["total"] for r in rows)
    req = sorted(r["total"] for r in rows if 20 <= r.get("chars", 0) <= 30)
    lines += ["", f"- 全件: 中央値 {_med(tot):.0f} ms、p95 {tot[min(len(tot) - 1, round(0.95 * (len(tot) - 1)))]:.0f} ms、"
              f"最大 {tot[-1]:.0f} ms"]
    if req:
        lines.append(f"- **20〜30 字（速度要件の範囲）: {len(req)} 回、中央値 {_med(req):.0f} ms、最大 {req[-1]:.0f} ms、"
                     f"1 秒を超えた回 {sum(x > 1000 for x in req)}**")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
