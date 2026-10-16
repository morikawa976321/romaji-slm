"""AI が書いた「正解の出力文と読み」から、本人の打ち方を真似た打鍵列を作って評価セットにする（docs/EVAL.md「本人の文体の合成セット」）。

    uv run python -m eval.build_synth <gen1.jsonl> [<gen2.jsonl> ...] --out eval/sets/synth_user.jsonl

入力の 1 行: {"category", "output", "reading"（ひらがな。英字・数字・記号は出力のまま）, "kind": sentence|fragment}
- 読みは SudachiPy（data.reading.analyze）と、書き手の読みを data.reading.align_zenz で突き合わせる。2 語を超えて食い違う文は捨てる
  （正解側の読みの誤りを評価に持ち込まない）。2 語以下の食い違いは書き手の読みを採る。
- 打鍵列は学習データと同じ道具（data.build.segment_pieces → data.romaji.romanize。表記揺れの乱数つき）。
  完結した文は半分の確率で「、」「。」を打たない（本人は記号を最小限にして打つ）。
- 完結した文には、打鍵ミス版（data.noise.add_noise、rate=1.0・最大 2 か所）を category=noisy で対にして足す。
"""

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

from data.build import _HAS_WORD, _TYPED_OK, segment_pieces, segment_reading
from data.noise import add_noise
from data.reading import Unreadable, align_zenz, analyze
from data.romaji import UnreadableKana, romanize
from data.symbols import is_typable


def to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def build_item(rec: dict, seed: int, stats: Counter) -> list[dict]:
    output = rec["output"].strip()
    try:
        tokens = analyze(output)
    except Unreadable as e:
        stats[f"excluded:{e.reason}"] += 1
        return []
    aligned = align_zenz(tokens, to_katakana(rec.get("reading", "")))
    if not aligned:
        stats["excluded:reading_mismatch"] += 1
        return []
    tokens, n_subst = aligned
    stats["reading_substituted"] += n_subst > 0
    rng = random.Random(f"{seed}:{output}")
    sentence = rec.get("kind") == "sentence"
    omit = sentence and rng.random() < 0.5
    try:
        # 省くのは句読点 B が補える「、」「。」だけ。括弧やかぎ括弧は本人も打つ（省くと正解に戻せない）
        typed = [t for t in tokens if not (omit and t.kind == "sym" and t.surface in "、。")]
        clean = romanize(segment_pieces(typed), rng)
    except UnreadableKana:
        stats["excluded:unreadable_kana"] += 1
        return []
    if not clean or not _TYPED_OK.match(clean) or not is_typable(clean) or not _HAS_WORD.search(clean):
        stats["excluded:not_typable"] += 1
        return []
    meta = {"category": rec["category"], "kind": rec.get("kind"), "omit_punct": omit, "reading": segment_reading(tokens)}
    items = [{"romaji": clean, "output": output, "left_context": "", "meta": meta}]
    if sentence:
        noisy, notes = add_noise(clean, rng, rate=1.0, max_ops=2)
        if noisy != clean:
            items.append({"romaji": noisy, "output": output, "left_context": "",
                          "meta": {**meta, "category": "noisy", "topic": rec["category"], "noise": notes, "pair_of": clean}})
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    stats: Counter = Counter()
    seen: set[str] = set()
    out = []
    for p in args.inputs:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["excluded:bad_json"] += 1
                continue
            if rec.get("output", "").strip() in seen:
                stats["excluded:duplicate"] += 1
                continue
            seen.add(rec.get("output", "").strip())
            stats["input"] += 1
            items = build_item(rec, args.seed, stats)
            stats["kept"] += bool(items)
            out += items
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    print(json.dumps({"items": len(out), **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
