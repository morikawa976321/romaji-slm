"""二段形式のモデルで、読みの段と変換の段のどちらで誤るかを切り分ける（AJIMEE は正解のカタカナ読みを持つ）。

    uv run python -m eval.reading_check --model models/zenz-romaji-v2 [--limit 200] [--device cuda]

読みの一致は、記号・長音・空白を無視して AJIMEE の input（カタカナ）と比べる。
変換の段の参考として、正解の読みを元の zenz-v3 形式でこのモデルに与えたときの出力も測る（読みが正しければ当たるか）。
"""

import argparse
import json
import sys

from .converters import HFCausalLM
from .datasets import load_ajimee
from .metrics import loose_normalize as loose_key
from .prompts import CONTEXT, INPUT, OUTPUT, build_prompt, split_two_stage


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    conv = HFCausalLM(args.model, "zenz-v3-romaji-reading", args.device)
    tok, torch = conv.tok, conv._torch

    def gen(prompt: str, cap: int) -> str:
        enc = tok(prompt, return_tensors="pt", add_special_tokens=False).to(args.device)
        with torch.no_grad():
            out = conv.model.generate(enc.input_ids, attention_mask=enc.attention_mask, max_new_tokens=cap,
                                      do_sample=False, eos_token_id=tok.eos_token_id,
                                      pad_token_id=tok.pad_token_id or tok.eos_token_id)
        return tok.decode(out[0][enc.input_ids.shape[1]:], skip_special_tokens=True)

    items = load_ajimee(use_context=True)[: args.limit]
    n_read = n_both = n_e2e = n_oracle = 0
    shown = 0
    for it in items:
        full = gen(build_prompt(conv.template, it.romaji, it.left_context), 3 * len(it.romaji) + 16)
        reading, text = split_two_stage(full)
        gold_kana = it.meta.get("kana", "")
        read_ok = loose_key(reading) == loose_key(gold_kana)
        e2e_ok = text in it.refs
        head = CONTEXT + it.left_context if it.left_context else ""
        oracle = gen(f"{head}{INPUT}{gold_kana}{OUTPUT}", 2 * len(gold_kana) + 16)
        n_read += read_ok
        n_e2e += e2e_ok
        n_both += read_ok and e2e_ok
        n_oracle += oracle in it.refs
        if not read_ok and shown < args.show:
            shown += 1
            print(f"- {it.romaji[:60]}\n  正解の読み: {gold_kana[:40]}\n  モデルの読み: {reading[:40]}\n  出力: {text[:40]}")
    n = len(items)
    print(json.dumps({"n": n, "reading_ok": n_read / n, "e2e_exact": n_e2e / n,
                      "e2e_exact_given_reading_ok": n_both / max(n_read, 1),
                      "oracle_reading_exact": n_oracle / n}, ensure_ascii=False))


if __name__ == "__main__":
    main()
