"""romaji2ja（limoXD/romaji2ja）の変換精度を測る。

使い方:
    uv run python eval/eval_romaji2ja.py [--device cuda|cpu] [--limit N]

結果は eval/out/romaji2ja_<set>_<mode>.jsonl に1件1行で書き、集計を標準出力に出す。
mode は normalized（配布元の正規化を通す。記号とスペースを捨てる）と raw（打鍵列そのまま）の2つ。
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, str(Path(__file__).parent))
from kana import kata_to_romaji  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "romaji2ja"
AJIMEE = ROOT / "models" / "ajimee" / "evaluation_items.json"
USER_SET = ROOT / "eval" / "sets" / "user_style.jsonl"
OUT = ROOT / "eval" / "out"

BOS_IN = ""
BOS_OUT = ""

# 配布元 code/normalization.py と同じ規則（NFKC・小文字化のうえ、空白とこの記号群を捨てる）
_SOFT_SEP = frozenset("-_'`\\/|.,;:!?()[]{}<>\"‐‑‒–—―−")


def normalize_like_upstream(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.strip()).lower()
    return "".join(
        ch for ch in text
        if not (ch.isspace() or ch in _SOFT_SEP or unicodedata.category(ch) == "Cf")
    )


_PUNCT = re.compile(r"[\s、。，．,.!！?？:：・「」\[\]（）()〜~ー-]")


def strip_punct(s: str) -> str:
    """句読点・記号・長音・空白と英字の大小を無視して比べるための正規化。"""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", s)).lower()


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def min_cer(refs: list[str], hyp: str) -> float:
    return min(levenshtein(r, hyp) / max(len(r), 1) for r in refs)


def load_sets(limit: int | None):
    items = json.loads(AJIMEE.read_text(encoding="utf-8"))
    ajimee = [
        {"id": it["index"], "cat": "ajimee_ctx" if it["context_text"] else "ajimee",
         "kana": it["input"], "input": kata_to_romaji(it["input"]), "expected": it["expected_output"]}
        for it in items
    ]
    user = [
        {"id": str(i), **json.loads(line)}
        for i, line in enumerate(USER_SET.read_text(encoding="utf-8").splitlines()) if line.strip()
    ]
    if limit:
        ajimee, user = ajimee[:limit], user[:limit]
    return {"ajimee": ajimee, "user": user}


def convert(model, tok, text: str, device: str) -> tuple[str, int]:
    prompt = BOS_IN + text + BOS_OUT
    enc = tok(prompt, return_tensors="pt", add_special_tokens=False).to(device)
    n_prompt = enc.input_ids.shape[1]
    cap = max(1, min(n_prompt + 32, 768, model.config.max_position_embeddings - n_prompt))
    with torch.no_grad():
        out = model.generate(
            enc.input_ids, attention_mask=enc.attention_mask, max_new_tokens=cap,
            do_sample=False, eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id,
        )
    gen = out[0][n_prompt:]
    return tok.decode(gen, skip_special_tokens=True), len(gen)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForCausalLM.from_pretrained(MODEL_DIR, dtype=torch.float32).to(args.device).eval()
    print(f"params={sum(p.numel() for p in model.parameters())/1e6:.1f}M device={args.device}")

    OUT.mkdir(parents=True, exist_ok=True)
    for set_name, items in load_sets(args.limit).items():
        for mode in ("normalized", "raw"):
            rows = []
            t0 = time.time()
            for it in items:
                text = normalize_like_upstream(it["input"]) if mode == "normalized" else it["input"]
                hyp, n_gen = convert(model, tok, text, args.device)
                rows.append({
                    **it, "model_input": text, "output": hyp, "n_gen_tokens": n_gen,
                    "exact": hyp in it["expected"],
                    "exact_nopunct": strip_punct(hyp) in {strip_punct(e) for e in it["expected"]},
                    "min_cer": min_cer(it["expected"], hyp),
                })
            dt = time.time() - t0
            path = OUT / f"romaji2ja_{set_name}_{mode}.jsonl"
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

            cats = sorted({r["cat"] for r in rows})
            print(f"\n[{set_name} / {mode}] n={len(rows)} ({dt:.1f}s)")
            for c in cats + ["ALL"]:
                rs = [r for r in rows if c == "ALL" or r["cat"] == c]
                print(f"  {c:11s} n={len(rs):3d}  exact={sum(r['exact'] for r in rs)/len(rs):6.1%}"
                      f"  exact_nopunct={sum(r['exact_nopunct'] for r in rs)/len(rs):6.1%}"
                      f"  minCER={sum(r['min_cer'] for r in rs)/len(rs):.3f}")


if __name__ == "__main__":
    main()
