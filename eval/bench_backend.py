"""変換コアの実行系（core = transformers、core-llama = llama-server）を同じ入力列で比べる。

    uv run python -m eval.bench_backend --gguf models/zenz-v3.1-small-Q8_0.gguf
    uv run python -m eval.bench_backend --gguf ... --converters "core;core-llama:np=5,t=8;core-llama:np=1,t=8"

入力は user_style のうち正解の字数が --min-chars〜--max-chars のもの。1件ごとの所要時間（変換1回の全体）の
中央値・p95 と、core-llama では文法の大きさの中央値と `/completion` の timings（1トークンあたりの ms）を表にする。
出力が core と一致した件数も数える（量子化・トークン分割の差でずれうる。docs/LLAMA_BACKEND.md）。
速度は測った機械と条件を添えて記録する（CLAUDE.md）。
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from .converters import make_converter
from .datasets import load_set
from .report import env_info

OUT_DIR = Path(__file__).resolve().parent / "out"


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def _med(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def run(spec: str, items, model: str | None, device: str, warmup: int) -> dict:
    conv = make_converter(spec, device, model)
    try:
        for _ in range(warmup):
            conv.convert("konnichiha")
        backend = getattr(conv, "backend", None)
        if backend is not None:
            backend.log.clear()
        outs, ms = [], []
        for it in items:
            t0 = time.perf_counter()
            outs.append(conv.convert(it.romaji, it.left_context))
            ms.append((time.perf_counter() - t0) * 1000)
        row = {"converter": spec, "n": len(items), "median_ms": _med(ms), "p95_ms": _pct(ms, 0.95), "outputs": outs,
               "ms": ms, "acc": sum(o in it.refs for o, it in zip(outs, items)) / max(1, len(items))}
        if backend is not None:
            dec = [e for e in backend.log if e["kind"] == "decode"]
            tm = [e["timings"] for e in backend.log]
            row.update({
                "grammar_rules_median": _med([e["grammar_rules"] for e in dec]),
                "grammar_bytes_median": _med([e["grammar_bytes"] for e in dec]),
                "prompt_ms_per_tok": _med([t["prompt_per_token_ms"] for t in tm if t.get("prompt_n")]),
                "gen_ms_per_tok": _med([t["predicted_per_token_ms"] for t in tm if t.get("predicted_n")]),
                "decode_req_ms_median": _med([e["ms"] for e in dec]),
                "fluency_req_ms_median": _med([e["ms"] for e in backend.log if e["kind"] == "fluency"]),
                "n_refill": backend.n_refill, "n_resplit": backend.n_resplit,
                "eos_logprob_included": backend.eos_logprob_included,
                "server": backend.server.url,
            })
        return row
    finally:
        if hasattr(conv, "close"):
            conv.close()


def render(rows: list[dict], env: dict, args) -> str:
    base = rows[0]["outputs"]
    lines = [f"# 実行系の比較（{time.strftime('%Y-%m-%d %H:%M')}）", "",
             f"- 機械: {env.get('cpu')}（論理 {env.get('logical_cpus')}）、{args.device}、"
             f"torch {env.get('torch', '-')}",
             f"- GGUF: {args.gguf}、HF: {args.hf_model}",
             f"- 入力: {args.set} の正解 {args.min_chars}〜{args.max_chars} 字、{rows[0]['n']} 件、"
             f"空回し {args.warmup} 回", "",
             "| 変換器 | 中央値 ms | p95 ms | 正解率 | 1位が先頭と一致 | 文法 規則/バイト（中央値） "
             "| prompt ms/tok | 生成 ms/tok | 生成1要求 ms | 自然さ1要求 ms | 取り直し |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        same = sum(a == b for a, b in zip(r["outputs"], base))
        g = (f"{r['grammar_rules_median']:.0f} / {r['grammar_bytes_median']:.0f}" if "grammar_rules_median" in r
             else "-")
        f = lambda k: f"{r[k]:.2f}" if k in r else "-"  # noqa: E731
        lines.append(f"| {r['converter']} | {r['median_ms']:.0f} | {r['p95_ms']:.0f} | {r['acc']:.1%} "
                     f"| {same}/{r['n']} | {g} | {f('prompt_ms_per_tok')} | {f('gen_ms_per_tok')} "
                     f"| {f('decode_req_ms_median')} | {f('fluency_req_ms_median')} "
                     f"| {r.get('n_refill', '-')}/{r.get('n_resplit', '-')} |")
    lines += ["", "取り直し = 載らなかった logprob の取り直し回数 / 自然さの分割の取り直し回数"
              "（docs/LLAMA_BACKEND.md）。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gguf", required=True, help="core-llama に渡す GGUF")
    ap.add_argument("--hf-model", default="Miwa-Keita/zenz-v3.1-small", help="core に渡す HF のモデル")
    ap.add_argument("--converters", default="core;core-llama:np=5",
                    help="変換器を `;` で区切る（名前の中のオプションがカンマ区切りのため）。"
                    "例: core;core-llama:np=5,t=8")
    ap.add_argument("--set", default="user_style")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=45)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--warmup", type=int, default=2)
    args = ap.parse_args(argv)

    items = [it for it in load_set(args.set) if args.min_chars <= len(it.refs[0]) <= args.max_chars]
    specs = [s for s in args.converters.split(";") if s]
    env = env_info(args.device)
    rows = []
    for spec in specs:
        model = args.gguf if spec.startswith("core-llama") else args.hf_model
        rows.append(run(spec, items, model, args.device, args.warmup))
        print(f"{spec}: 中央値 {rows[-1]['median_ms']:.0f} ms", file=sys.stderr)
    md = render(rows, env, args)
    print(md)
    OUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (OUT_DIR / f"bench_backend-{stamp}.md").write_text(md, encoding="utf-8")
    (OUT_DIR / f"bench_backend-{stamp}.json").write_text(
        json.dumps({"env": env, "args": vars(args), "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
