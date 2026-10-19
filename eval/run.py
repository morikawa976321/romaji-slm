"""評価の実行。

    uv run python -m eval.run --converter zenz-baseline --set ajimee --limit 20 --device cuda
"""

import argparse
import sys

from .converters import Converter, make_converter, timed
from .datasets import Item, load_set
from .metrics import english_kept, score
from .report import env_info, render_markdown, write_results


def evaluate(conv: Converter, items: list[Item]) -> list[dict]:
    rows = []
    for it in items:
        res, ms = timed(conv, it.romaji, it.left_context)
        rows.append({
            "id": it.id, "category": it.category, "romaji": it.romaji, "left_context": it.left_context,
            "refs": it.refs, "output": res.text, **score(it.refs, res.text),
            "oracle": any(c in it.refs for c in res.candidates) if res.candidates else None,
            "en_kept": english_kept(it.refs[0], res.text),
            "edited": res.edited,
            "ms": ms, "n_prompt_tokens": res.n_prompt_tokens, "n_gen_tokens": res.n_gen_tokens,
            "prompt": res.prompt,
        })
    return rows


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--converter", required=True, help="romaji2ja / romaji2ja-raw / zenz-baseline / "
                    "zenz-xsmall-baseline / zenz-romaji / hf")
    ap.add_argument("--set", action="append", required=True, dest="sets",
                    help="ajimee / ajimee-noctx / user_style / file:<jsonl>（繰り返し可）")
    ap.add_argument("--model", help="モデルのパスまたは HF の ID")
    ap.add_argument("--template", help="プロンプト雛形（prompts.py）")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--device", default="cuda", help="torch の変換器は cuda だけ（CPU では core-ort を使う）")
    ap.add_argument("--warmup", type=int, default=2, help="計測前の空回し回数（初回の遅さを除く）")
    args = ap.parse_args(argv)

    conv = make_converter(args.converter, args.device, args.model, args.template)
    env = env_info(args.device)
    for _ in range(args.warmup):
        conv.convert("konnichiha")
    for set_name in args.sets:
        rows = evaluate(conv, load_set(set_name, args.limit))
        jsonl, md = write_results(conv.name, set_name, rows, env)
        print(render_markdown(conv.name, set_name, rows, env))
        print(f"-> {jsonl}\n-> {md}\n")


if __name__ == "__main__":
    main()
