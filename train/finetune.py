"""zenz を、ローマ字の打鍵列を直接受けるように微調整する（DESIGN.md 方式3）。

    uv run python -m train.finetune --train data/out300k/train.jsonl --valid data/out300k/valid.jsonl \
        --out models/zenz-romaji-v1

入力は学習データの共有 JSONL（docs/DATA.md）。プロンプトは評価と同じ `eval.prompts` の `zenz-v3-romaji` で組み、
その後ろに output と EOS を続ける。損失は output と EOS の部分だけで取る（プロンプト部分は -100）。
"""

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.prompts import build_prompt  # noqa: E402

TEMPLATE = "zenz-v3-romaji"


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def encode(tok, rec: dict, max_len: int) -> tuple[list[int], list[int]] | None:
    """(input_ids, labels)。長すぎるときは左文脈を先頭側から削る。それでも入らなければ None。"""
    out_ids = tok(rec["output"], add_special_tokens=False).input_ids + [tok.eos_token_id]
    ctx = rec.get("left_context") or ""
    while True:
        p_ids = tok(build_prompt(TEMPLATE, rec["romaji"], ctx), add_special_tokens=False).input_ids
        if len(p_ids) + len(out_ids) <= max_len:
            return p_ids + out_ids, [-100] * len(p_ids) + out_ids
        if not ctx:
            return None
        # 1トークンは平均1〜2字なので、超過トークン数ぶんの字を削れば概ね収まる
        over = len(p_ids) + len(out_ids) - max_len
        ctx = ctx[max(over, 1):]


def batches(data: list[tuple[list[int], list[int]]], batch_tokens: int, rng: random.Random):
    """長さの近いものを束ねて、パディングを減らす（バッチ内トークン数 ≲ batch_tokens）。"""
    idx = sorted(range(len(data)), key=lambda i: len(data[i][0]) + rng.random())
    out, cur, cur_max = [], [], 0
    for i in idx:
        n = len(data[i][0])
        if cur and max(cur_max, n) * (len(cur) + 1) > batch_tokens:
            out.append(cur)
            cur, cur_max = [], 0
        cur.append(i)
        cur_max = max(cur_max, n)
    if cur:
        out.append(cur)
    rng.shuffle(out)
    return out


def collate(data, ids, pad_id, device):
    L = max(len(data[i][0]) for i in ids)
    x = torch.full((len(ids), L), pad_id, dtype=torch.long)
    y = torch.full((len(ids), L), -100, dtype=torch.long)
    m = torch.zeros((len(ids), L), dtype=torch.long)
    for r, i in enumerate(ids):
        a, b = data[i]
        x[r, :len(a)] = torch.tensor(a)
        y[r, :len(b)] = torch.tensor(b)
        m[r, :len(a)] = 1
    return x.to(device), y.to(device), m.to(device)


@torch.no_grad()
def evaluate(model, data, batch_tokens, pad_id, device) -> float:
    model.eval()
    tot, n = 0.0, 0
    for ids in batches(data, batch_tokens, random.Random(0)):
        x, y, m = collate(data, ids, pad_id, device)
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
            logits = model(input_ids=x, attention_mask=m).logits
        loss = torch.nn.functional.cross_entropy(
            logits[:, :-1].float().reshape(-1, logits.size(-1)), y[:, 1:].reshape(-1),
            ignore_index=-100, reduction="sum")
        tot += loss.item()
        n += (y[:, 1:] != -100).sum().item()
    model.train()
    return tot / max(n, 1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="Miwa-Keita/zenz-v3.1-small")
    ap.add_argument("--train", required=True)
    ap.add_argument("--valid", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--batch-tokens", type=int, default=16384)
    ap.add_argument("--max-len", type=int, default=256)
    ap.add_argument("--eval-every", type=int, default=1000)
    ap.add_argument("--log-every", type=int, default=100)
    ap.add_argument("--max-valid", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    tok = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForCausalLM.from_pretrained(args.base, dtype=torch.float32).to(device)
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    def prep(path, limit=None):
        recs = load_jsonl(path)
        if limit:
            recs = recs[:limit]
        enc = [encode(tok, r, args.max_len) for r in recs]
        kept = [e for e in enc if e]
        print(f"{path}: {len(kept)}/{len(recs)} 件（長すぎて捨てた {len(recs) - len(kept)}）", flush=True)
        return kept

    t0 = time.time()
    train, valid = prep(args.train), prep(args.valid, args.max_valid)
    n_tok = sum(len(a) for a, _ in train)
    print(f"tokenize {time.time() - t0:.0f}s, train tokens={n_tok:,}", flush=True)

    steps_per_epoch = len(batches(train, args.batch_tokens, random.Random(0)))
    total = math.ceil(steps_per_epoch * args.epochs)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01, betas=(0.9, 0.98))
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / args.warmup) * 0.5 * (1 + math.cos(math.pi * min(s, total) / total)))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "train_log.jsonl", "a", encoding="utf-8")
    best = evaluate(model, valid, args.batch_tokens, pad_id, device)
    print(f"step 0 valid_loss={best:.4f}", flush=True)
    log.write(json.dumps({"step": 0, "valid_loss": best}) + "\n")

    model.train()
    step, t0, run_loss = 0, time.time(), 0.0
    while step < total:
        for ids in batches(train, args.batch_tokens, rng):
            if step >= total:
                break
            x, y, m = collate(train, ids, pad_id, device)
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
                logits = model(input_ids=x, attention_mask=m).logits
            loss = torch.nn.functional.cross_entropy(
                logits[:, :-1].float().reshape(-1, logits.size(-1)), y[:, 1:].reshape(-1), ignore_index=-100)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            run_loss += loss.item()
            if step % args.log_every == 0:
                print(f"step {step}/{total} loss={run_loss / args.log_every:.4f} lr={sched.get_last_lr()[0]:.2e} "
                      f"{(time.time() - t0) / step:.3f}s/step", flush=True)
                run_loss = 0.0
            if step % args.eval_every == 0 or step == total:
                vl = evaluate(model, valid, args.batch_tokens, pad_id, device)
                print(f"step {step} valid_loss={vl:.4f}", flush=True)
                log.write(json.dumps({"step": step, "valid_loss": vl, "elapsed_s": round(time.time() - t0)}) + "\n")
                log.flush()
                if vl < best:
                    best = vl
                    model.save_pretrained(out)
                    tok.save_pretrained(out)
    (out / "train_args.json").write_text(json.dumps({**vars(args), "best_valid_loss": best, "steps": total},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done best_valid_loss={best:.4f} -> {out}", flush=True)


if __name__ == "__main__":
    main()
