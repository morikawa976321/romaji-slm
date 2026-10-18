"""候補ウィンドウ（core.window）の並べ方の比較。docs/decisions.md D37。

    uv run python -m eval.window_eval --device cuda                 # 全セットの候補を集めて eval/out/window/ に保存し、比べる
    uv run python -m eval.window_eval --from-saved                  # 保存した候補で並べ方だけを比べ直す（zenz を動かさない）
    uv run python -m eval.window_eval --sets short --show 10        # 窓の中身を表示

候補（core.window.pool）は入力1つにつき1回だけ集め、並べ方（PLANS）はそこから作る。1位はどの並べ方でも同じ。

測るもの:
- top1: 窓の1位が正解（どの並べ方でも同じ。変わっていないことの確認）
- in_win: 正解が窓（最大6件）に入る率。typo_hard は正解が読み（カタカナ）なので、窓のどれかが正しい読みから来たか
- typed: 窓のうち、打鍵を直していない読みから来た候補の割合
- short_words（自作の短い単語）: want（1位以外の欲しい書き方）が窓に入った割合と、最初の want の窓の中の順位
"""

import argparse
import json
import random
import statistics
from pathlib import Path

from core.window import Entry, Pool, arrange, pool
from eval.datasets import ROOT, SETS, Item, load_jsonl, load_set
from eval.reading_bench import TYPO_HARD_PATH, noisy, norm

OUT = ROOT / "eval" / "out" / "window"
SHORT_PATH = SETS / "short_words.jsonl"

# 並べ方の案: 名前 → (order, edit_slots を打鍵列から決める関数)
PLANS = {
    "reading": ("reading", lambda s: 0),
    "same_first": ("same_first", lambda s: 0),
    "merge": ("merge", lambda s: 0),
    "quota1": ("quota", lambda s: 1),
    "quota2": ("quota", lambda s: 2),
}


def load_items(name: str) -> list[Item]:
    if name == "ajimee":
        return load_set("ajimee")
    if name in ("ajimee_typo", "wac_typo"):
        base = load_set("ajimee" if name == "ajimee_typo" else "wac_test")
        rng = random.Random(0)   # eval.reading_bench と同じ（seed 0、項目の順に共有）
        out = []
        for it in base:
            s, notes = noisy(it.romaji, rng)
            out.append(Item(it.id, s, it.refs, it.left_context, "typo", {"clean": it.romaji, "noise": notes}))
        return out
    if name == "typo_hard":
        return load_jsonl(TYPO_HARD_PATH, "hard")
    if name == "user_style":
        return load_set("user_style")
    if name == "gen_noisy":
        return [it for it in load_set("gen_test") if it.category == "noisy"]
    if name in ("short", "short_typo"):
        rows = [json.loads(l) for l in SHORT_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        items = [Item(f"short{i}", d["romaji"], [], "", "short", {"want": d["want"]}) for i, d in enumerate(rows)]
        if name == "short":
            return items
        # 短い単語に打鍵ミスを 1〜2 個。正解はその読みの書き方のどれか（want）
        rng = random.Random(0)
        return [Item(it.id, noisy(it.romaji, rng)[0], it.meta["want"], "", "typo", {"clean": it.romaji})
                for it in items]
    raise ValueError(name)


SET_NAMES = ("short", "short_typo", "ajimee", "ajimee_typo", "wac_typo", "typo_hard", "user_style", "gen_noisy")


def _e(d) -> Entry:
    return Entry(**d)


def to_json(it: Item, pl: Pool | None) -> dict:
    row = {"id": it.id, "romaji": it.romaji, "refs": it.refs, "category": it.category, "meta": it.meta}
    if pl is not None:
        row.update(first=vars(pl.first), hira=vars(pl.hira) if pl.hira else None,
                   entries=[vars(e) for e in pl.entries], ms=pl.ms)
    return row


def from_json(row: dict) -> Pool | None:
    if "first" not in row:
        return None
    return Pool(_e(row["first"]), _e(row["hira"]) if row["hira"] else None, [_e(e) for e in row["entries"]], row["ms"])


def collect(names, device: str) -> None:
    from core.pipeline import CorePipeline
    p = CorePipeline(device=device)   # 常駐入力と同じ既定（句読点 B・英字は打ったまま）。ユーザー辞書は読まない
    OUT.mkdir(parents=True, exist_ok=True)
    pool(p, "konnichiha", strict=True)
    for name in names:
        items = load_items(name)
        rows = []
        for it in items:
            rows.append(to_json(it, pool(p, it.romaji, it.left_context, strict=True)))
        (OUT / f"{name}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                           encoding="utf-8")
        print(f"collected {name}: {len(rows)}", flush=True)


def _hit(row: dict, win: list[Entry], name: str) -> bool:
    if name == "typo_hard":
        ref = norm(row["meta"]["reading"] if "reading" in row["meta"] else "")
        return any(norm(e.reading) == ref for e in win)
    return any(e.text in row["refs"] for e in win)


def evaluate(names, show: int = 0) -> None:
    for name in names:
        path = OUT / f"{name}.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if name == "typo_hard":
            raw = [json.loads(l) for l in TYPO_HARD_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
            for r, d in zip(rows, raw):
                r["meta"]["reading"] = d["reading"]
        pools = [from_json(r) for r in rows]
        ms_c = [pl.ms["candidates"] for pl in pools if pl]
        ms_a = [pl.ms["alt"] for pl in pools if pl]
        print(f"\n## {name}（{len(rows)} 件。候補を集める時間 中央値 {statistics.median(ms_c):.0f} ms、"
              f"うち別の書き方 {statistics.median(ms_a):.0f} ms）")
        head = "| 並べ方 | top1 | in_win | typed |" + (" want | 最初の want の順位 | want が窓に無い語 |" if name == "short" else "")
        mixed = name == "user_style"   # 正常入力（typo 以外）の1位・誤補正と、typo の行の in_win を分けて出す
        if mixed:
            head += " 正常入力の top1 | 誤補正 | typo の in_win |"
        print(head)
        print("|" + "---|" * (head.count("|") - 1))
        tops = None
        for plan, (order, slots) in PLANS.items():
            n = top1 = hit = 0
            typed = []
            want_in = want_all = 0
            ranks = []
            none = 0
            t1 = []
            sub = {"clean": 0, "clean_top1": 0, "misfix": 0, "typo": 0, "typo_hit": 0}
            for r, pl in zip(rows, pools):
                if pl is None:
                    t1.append(None)
                    continue
                win = arrange(pl, order, 6, slots(r["romaji"]))
                n += 1
                t1.append(win[0].text)
                top1 += _hit(r, win[:1], name)
                hit += _hit(r, win, name)
                typed.append(sum(not e.edited for e in win) / len(win))
                if mixed and r["category"] == "typo":
                    sub["typo"] += 1
                    sub["typo_hit"] += _hit(r, win, name)
                elif mixed:
                    sub["clean"] += 1
                    sub["clean_top1"] += _hit(r, win[:1], name)
                    sub["misfix"] += win[0].edited
                if name == "short":
                    texts = [e.text for e in win]
                    want = [w for w in r["meta"]["want"] if w != win[0].text]
                    want_all += len(want)
                    got = [texts.index(w) + 1 for w in want if w in texts]
                    want_in += len(got)
                    if got:
                        ranks.append(min(got))
                    else:
                        none += 1
            tops = tops or t1
            assert t1 == tops, "1位が並べ方で変わった"
            line = (f"| {plan} | {top1}/{n} | {hit}/{n} ({hit / n:.1%}) | {statistics.mean(typed):.0%} |")
            if name == "short":
                line += (f" {want_in}/{want_all} ({want_in / want_all:.0%}) | "
                         f"{statistics.mean(ranks):.2f} | {none} |")
            if mixed:
                line += (f" {sub['clean_top1']}/{sub['clean']} | {sub['misfix']}/{sub['clean']} | "
                         f"{sub['typo_hit']}/{sub['typo']} |")
            print(line)
        for r, pl in list(zip(rows, pools))[:show]:
            if pl is None:
                continue
            print(f"\n{r['romaji']}")
            for plan, (order, slots) in PLANS.items():
                win = arrange(pl, order, 6, slots(r["romaji"]))
                print(f"  {plan:<10} " + " / ".join(e.text + ("*" if e.kind == "edit" else "") for e in win))


def timing(names, device: str, repeat: int, limit: int | None = None) -> None:
    """1 回の変換の所要: 読みの候補だけ（置き換える前と同じ計算）と、別の書き方を足したもの。

    同じ入力で両方を交互に（先に回す側を入れ替えて）測る。他の処理と CPU を取り合っても、差は比べられるようにするため。"""
    from core.pipeline import CorePipeline
    p = CorePipeline(device=device)
    for _ in range(3):
        pool(p, "konnichiha", strict=True)
    for name in names:
        before, after, alt = [], [], []
        for k, it in enumerate(load_items(name)[:limit]):
            for rep in range(repeat):
                for with_alt in ((False, True) if (k + rep) % 2 == 0 else (True, False)):
                    pl = pool(p, it.romaji, it.left_context, alts=with_alt, strict=True)
                    if pl is None:
                        continue
                    tot = pl.ms["candidates"] + pl.ms["alt"]
                    (after if with_alt else before).append(tot)
                    if with_alt:
                        alt.append(pl.ms["alt"])

        def q(xs, f):
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(f * len(xs)))]
        print(f"{name}（{device}、{len(before)} 回）: 読みの候補だけ 中央値 {statistics.median(before):.0f} ms・"
              f"p90 {q(before, 0.9):.0f} ms／別の書き方を足して 中央値 {statistics.median(after):.0f} ms・"
              f"p90 {q(after, 0.9):.0f} ms（足した分 中央値 {statistics.median(alt):.0f} ms・p90 {q(alt, 0.9):.0f} ms）",
              flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="*", default=list(SET_NAMES))
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--time", type=int, default=0, help="所要だけを、入力ごとにこの回数ずつ測る")
    ap.add_argument("--limit", type=int, default=None, help="--time で各セットの先頭何件を測るか")
    ap.add_argument("--from-saved", action="store_true")
    ap.add_argument("--show", type=int, default=0)
    args = ap.parse_args(argv)
    if args.time:
        return timing(args.sets, args.device, args.time, args.limit)
    if not args.from_saved:
        collect(args.sets, args.device)
    evaluate(args.sets, args.show)


if __name__ == "__main__":
    main()
