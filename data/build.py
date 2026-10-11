"""学習データを生成する。

    uv run python -m data.build --limit 20000 --out data/out/ --seed 0

出力は {out}/{train,valid,test}.jsonl と stats.json。形式は docs/DATA.md の契約どおり。
"""

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

from data.noise import add_noise
from data.reading import Token, Unreadable, align_zenz, analyze
from data.romaji import UnreadableKana, romanize
from data.symbols import is_typable

HF_DATASET = "Miwa-Keita/zenz-v2.5-dataset"
FIXTURE = Path(__file__).parent / "fixtures" / "zenz_like.jsonl"

_SENT_END = ("。", "！", "？", "!", "?", "．")
_CLOSERS = set("」』）)】］]”’")
_OPENERS = set("「『（(【［[“‘")
_CLAUSE_END = ("、", "，", ",")
_TYPED_OK = re.compile(r"^[!-~]+$")


def split_of(inp: str, out: str, valid: float, test: float) -> str:
    """元データ1件ごとのハッシュで決める。同じ (input, output) は常に同じ分割になる。"""
    h = int(hashlib.sha256(f"{inp}\t{out}".encode()).hexdigest()[:8], 16) / 0x100000000
    return "test" if h < test else "valid" if h < test + valid else "train"


def split_segments(tokens: list[Token], max_chars: int) -> tuple[list[list[Token]], int]:
    """1文ずつに切り、長すぎる文は読点で切る。(区間の列, 長すぎて捨てた区間数) を返す。"""
    sentences: list[list[Token]] = []
    cur: list[Token] = []
    pending = False  # 文末記号を見たので、直後の閉じ括弧までで切る
    depth = 0  # 括弧の中の句点では切らない（「いいね。」と言った。）
    for i, t in enumerate(tokens):
        cur.append(t)
        depth = max(0, depth + sum(c in _OPENERS for c in t.surface) - sum(c in _CLOSERS for c in t.surface))
        if t.surface.strip().endswith(_SENT_END) and depth == 0:
            pending = True
        nxt = tokens[i + 1].surface if i + 1 < len(tokens) else ""
        if pending and depth == 0 and not (nxt and all(c in _CLOSERS for c in nxt)):
            sentences.append(cur)
            cur, pending = [], False
    if cur:
        sentences.append(cur)

    def length(ts: list[Token]) -> int:
        return len("".join(t.surface for t in ts).strip())

    segs: list[list[Token]] = []
    dropped = 0
    for sent in sentences:
        if length(sent) <= max_chars:
            segs.append(sent)
            continue
        piece: list[Token] = []
        pieces: list[list[Token]] = []
        for t in sent:
            piece.append(t)
            if t.surface.endswith(_CLAUSE_END):
                pieces.append(piece)
                piece = []
        if piece:
            pieces.append(piece)
        merged: list[Token] = []
        for p in pieces:
            if merged and length(merged + p) > max_chars:
                segs.append(merged)
                merged = []
            merged = merged + p
        if merged:
            segs.append(merged)
    kept = []
    for s in segs:
        if length(s) > max_chars:
            dropped += 1
        else:
            kept.append(s)
    return kept, dropped


def segment_pieces(tokens: list[Token]) -> list[tuple[str, str]]:
    pieces: list[tuple[str, str]] = []
    for t in tokens:
        if t.kind == "ja":
            kind, text = "kana", t.reading
        elif t.kind in ("lit", "sym"):
            kind, text = "lit", t.typed
        else:
            continue
        if pieces and pieces[-1][0] == kind == "kana":
            pieces[-1] = (kind, pieces[-1][1] + text)
        else:
            pieces.append((kind, text))
    return pieces


def process_record(rec: dict, args: argparse.Namespace, stats: Counter) -> list[dict]:
    """元データ1件 → 学習サンプル0件以上。除外は stats["excluded:理由"] に数える。"""
    zenz_in, output = rec["input"], rec["output"].strip()
    left = rec.get("left_context") or ""
    if not output:
        stats["excluded:empty"] += 1
        return []
    try:
        tokens = analyze(output)
    except Unreadable as e:
        stats[f"excluded:{e.reason}"] += 1
        return []

    has_lit = any(t.kind == "lit" for t in tokens)
    aligned = align_zenz(tokens, zenz_in)
    if aligned:
        tokens, n_subst = aligned
        reading_from = "zenz"
        stats["zenz_substituted_tokens"] += n_subst
        stats["records_with_zenz_substitution"] += n_subst > 0
    elif has_lit or args.keep_mismatch:
        reading_from = "analyzer"
    else:
        stats["excluded:reading_mismatch"] += 1
        return []

    segs, dropped = split_segments(tokens, args.max_chars)
    stats["excluded_segments:too_long"] += dropped
    key = f"{args.seed}:{zenz_in}\t{output}"
    rng = random.Random(key)
    out: list[dict] = []
    ctx = left
    for seg in segs:
        text = "".join(t.surface for t in seg).strip()
        try:
            clean = romanize(segment_pieces(seg), rng)
        except UnreadableKana:
            stats["excluded_segments:unreadable_kana"] += 1
            ctx += text
            continue
        if not clean or not _TYPED_OK.match(clean) or not is_typable(clean):
            stats["excluded_segments:no_typed_text"] += 1
            ctx += text
            continue
        romaji, notes = add_noise(clean, rng, args.noise_rate, args.noise_max)
        out.append({
            "romaji": romaji,
            "output": text,
            "left_context": ctx[-args.max_left:] if args.max_left else ctx,
            "meta": {"source": rec.get("source", HF_DATASET), "noise": notes, "reading_from": reading_from},
        })
        ctx += text
    if out:
        stats["records_kept"] += 1
        stats[f"reading_from:{reading_from}"] += 1
    else:
        stats["excluded:no_segment"] += 1
    return out


def iter_records(source: str, seed: int, shuffle_buffer: int) -> Iterator[dict]:
    if source == "hf":
        from datasets import load_dataset

        ds = load_dataset(HF_DATASET, split="train", streaming=True)
        if shuffle_buffer:
            ds = ds.shuffle(seed=seed, buffer_size=shuffle_buffer)
        for row in ds:
            yield {"input": row["input"], "output": row["output"], "left_context": row.get("left_context") or ""}
        return
    path = FIXTURE if source == "fixture" else Path(source)
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def build(records: Iterable[dict], args: argparse.Namespace) -> Counter:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {k: open(out_dir / f"{k}.jsonl", "w", encoding="utf-8") for k in ("train", "valid", "test")}
    stats: Counter = Counter()
    try:
        for rec in records:
            if stats["samples"] >= args.limit or stats["records_read"] >= args.max_records:
                break
            stats["records_read"] += 1
            split = split_of(rec["input"], rec["output"].strip(), args.valid_ratio, args.test_ratio)
            for s in process_record(rec, args, stats):
                if stats["samples"] >= args.limit:
                    break
                files[split].write(json.dumps(s, ensure_ascii=False) + "\n")
                stats["samples"] += 1
                stats[f"split:{split}"] += 1
                if s["meta"]["noise"]:
                    stats["noisy"] += 1
    finally:
        for f in files.values():
            f.close()
    (out_dir / "stats.json").write_text(json.dumps(dict(sorted(stats.items())), ensure_ascii=False, indent=2) + "\n")
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=20000, help="出力するサンプル数の上限")
    p.add_argument("--out", default="data/out/")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--source", default="hf", help="hf | fixture | JSONL のパス（input/output/left_context）")
    p.add_argument("--max-records", type=int, default=10**9, help="読む元データ件数の上限")
    p.add_argument("--shuffle-buffer", type=int, default=10000, help="hf streaming のシャッフル用バッファ（0で無効）")
    p.add_argument("--noise-rate", type=float, default=0.3)
    p.add_argument("--noise-max", type=int, default=2)
    p.add_argument("--max-chars", type=int, default=60, help="1サンプルの output の最大字数")
    p.add_argument("--max-left", type=int, default=200, help="left_context の最大字数（末尾側を残す。0で無制限）")
    p.add_argument("--valid-ratio", type=float, default=0.02)
    p.add_argument("--test-ratio", type=float, default=0.02)
    p.add_argument("--keep-mismatch", action="store_true", help="英数字を含まない文で読みが zenz と食い違っても解析器の読みで残す")
    args = p.parse_args(argv)

    try:
        stats = build(iter_records(args.source, args.seed, args.shuffle_buffer), args)
    except Exception as e:  # HF に届かないときなど
        print(f"失敗: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(dict(sorted(stats.items())), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
