"""読みラティス（core/reading.py）の評価。docs/REDESIGN.md 工程2の合格条件を測る。

    uv run python -m eval.reading_bench            # AJIMEE（きれい・打鍵ミス入り）、user_style、raw_tokens
    uv run python -m eval.reading_bench --fails 10 # 失敗例も表示

AJIMEE のローマ字は eval/kana.py で正解のカタカナから作ったもの。打鍵ミス版は data/noise.py で1〜2個入れる
（seed 固定）。比較は記号と長音を除いたカタカナどうし。
"""

import argparse
import json
import random
import re
import statistics
import time
from pathlib import Path

from core.reading import readings
from data.noise import add_noise
from eval.datasets import ROOT, Item, load_ajimee
from eval.kana import kata_to_romaji

USER_READING_PATH = ROOT / "eval" / "sets" / "user_style_reading.jsonl"
USER_STYLE_PATH = ROOT / "eval" / "sets" / "user_style.jsonl"
RAW_TOKENS_PATH = ROOT / "eval" / "sets" / "raw_tokens.jsonl"
_LEN_BINS = ((0, 20), (20, 40), (40, 60), (60, 10**9))
_STRIP = re.compile(r"[ー、。，．・「」『』（）()！？!?：:…\[\]/,.\-~〜]")


def norm(s: str) -> str:
    return _STRIP.sub("", s)


def _rank(cands: list[str], ref: str) -> int | None:
    for k, c in enumerate(cands, 1):
        if c == ref:
            return k
    return None


def noisy(romaji: str, rng: random.Random) -> tuple[str, list[str]]:
    """打鍵ミスを必ず1〜2個入れる（入らなければ入るまで引き直す）。"""
    for _ in range(20):
        s, notes = add_noise(romaji, rng, rate=1.0, max_ops=2)
        if notes:
            return s, notes
    return romaji, []


def load_wac_test(limit: int = 200) -> list[Item]:
    """コスト調整用: Wikipedia 注釈コーパスの test（言語モデルの学習に使っていない）。かなと句読点だけの文。"""
    from core.kana_lm import to_katakana, wac_sentences

    items = []
    for sent in wac_sentences(("test",)):
        kana = "".join(to_katakana(r) if re.fullmatch(r"[ぁ-ゖァ-ヴー]+", to_katakana(r)) else sur for sur, r in sent)
        if not re.fullmatch(r"[ァ-ヴー、。「」・（）]+", kana) or not 10 <= len(kana) <= 45:
            continue
        items.append(Item(id=str(len(items)), romaji=kata_to_romaji(kana), refs=[], meta={"kana": kana}))
    return items[:limit]


def run_ajimee(n: int, seed: int, fails: int, items: list[Item] | None = None) -> dict:
    items = items if items is not None else load_ajimee(False)
    rng = random.Random(seed)
    res = {"clean_top1": 0, "clean_misfix": 0, "noisy_top1": 0, "noisy_top5": 0, "noisy_topn": 0, "total": len(items)}
    times: list[float] = []
    by_len: dict[tuple[int, int], list[float]] = {b: [] for b in _LEN_BINS}
    fail_clean, fail_noisy = [], []

    def timed(keys: str):
        t = time.perf_counter()
        rs = readings(keys, n)
        dt = time.perf_counter() - t
        times.append(dt)
        by_len[next(b for b in _LEN_BINS if b[0] <= len(keys) < b[1])].append(dt)
        return rs

    for it in items:
        ref = norm(it.meta["kana"])
        rs = timed(it.romaji)
        if rs and norm(rs[0].zenz_input()) == ref:
            res["clean_top1"] += 1
        else:
            fail_clean.append((it.romaji, it.meta["kana"], rs[0] if rs else None))
        if rs and rs[0].edits:
            res["clean_misfix"] += 1
        typo, notes = noisy(it.romaji, rng)
        rs = timed(typo)
        k = _rank([norm(r.zenz_input()) for r in rs], ref)
        res["noisy_top1"] += k == 1
        res["noisy_top5"] += k is not None and k <= 5
        res["noisy_topn"] += k is not None
        if k is None or k > 5:
            fail_noisy.append((typo, notes, it.meta["kana"], rs[0] if rs else None, k))
    res["ms_mean"] = 1000 * statistics.mean(times)
    res["ms_p50"] = 1000 * statistics.median(times)
    res["ms_p95"] = 1000 * sorted(times)[int(len(times) * 0.95)]
    res["ms_max"] = 1000 * max(times)
    res["by_len"] = {b: _ms(v) for b, v in by_len.items() if v}
    res["fail_clean"] = fail_clean[:fails]
    res["fail_noisy"] = fail_noisy[:fails]
    return res


def _ms(times: list[float]) -> dict:
    ts = sorted(times)
    return {"n": len(ts), "p50": 1000 * statistics.median(ts), "p95": 1000 * ts[min(len(ts) - 1, int(len(ts) * 0.95))]}


def run_misfix(path: Path = USER_STYLE_PATH) -> dict:
    """user_style の typo 以外（正しく打った文）で、1位が編集を含む件数。"""
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [d for d in rows if d["meta"]["category"] != "typo"]
    bad = []
    for d in rows:
        rs = readings(d["romaji"], 1)
        if rs and rs[0].edits:
            bad.append((d["romaji"], rs[0]))
    return {"n": len(rows), "misfix": len(bad), "fails": bad}


def run_raw(n: int, fails: int, path: Path = RAW_TOKENS_PATH) -> dict:
    """辞書に無い英字列（略語・型番）を含む文で、打ったとおりの読みが1位・上位5に入るか。"""
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by: dict[str, dict] = {}
    failed = []
    for d in rows:
        rs = readings(d["romaji"], n)
        k = _rank([r.zenz_input() for r in rs], d["reading"])
        for key in (d["meta"]["category"], "all"):
            b = by.setdefault(key, {"n": 0, "top1": 0, "top5": 0})
            b["n"] += 1
            b["top1"] += k == 1
            b["top5"] += k is not None and k <= 5
        if k is None or k > 5:
            failed.append((d["romaji"], d["reading"], rs[0] if rs else None, k))
    return {"by": by, "fails": failed[:fails]}


def _en_segs(reading: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9.\-]*", reading)


def run_user(n: int, fails: int, path: Path = USER_READING_PATH) -> dict:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by: dict[str, dict] = {}
    failed = []
    for d in rows:
        cat = d["meta"]["category"]
        rs = readings(d["romaji"], n)
        cands = [r.zenz_input() for r in rs]
        k = _rank(cands, d["reading"])
        want_en = [w for w in _en_segs(d["reading"]) if not w[0].isdigit()]
        en_ok = any([s.text for s in r.segs if s.kind == "en"] == want_en for r in rs[:5])
        for key in (cat, "all"):
            b = by.setdefault(key, {"n": 0, "top1": 0, "top5": 0, "en_top5": 0, "has_en": 0, "misfix": 0})
            b["n"] += 1
            b["top1"] += k == 1
            b["top5"] += k is not None and k <= 5
            if want_en:
                b["has_en"] += 1
                b["en_top5"] += en_ok
            if cat != "typo" and rs and rs[0].edits:
                b["misfix"] += 1
        if k != 1:
            failed.append((d["romaji"], d["reading"], rs[0] if rs else None, k))
    return {"by": by, "fails": failed[:fails]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fails", type=int, default=0)
    ap.add_argument("--skip-ajimee", action="store_true")
    ap.add_argument("--set", default="ajimee", choices=["ajimee", "wac-test"])
    args = ap.parse_args()
    readings("warmup")
    if not args.skip_ajimee:
        a = run_ajimee(args.n, args.seed, args.fails, load_wac_test() if args.set == "wac-test" else None)
        t = a["total"]
        print(f"{args.set} {t}件  きれい: 1位 {a['clean_top1']}/{t}  誤補正 {a['clean_misfix']}/{t}")
        print(f"         打鍵ミス1〜2個: 1位 {a['noisy_top1']}/{t}  上位5 {a['noisy_top5']}/{t}  上位{args.n} {a['noisy_topn']}/{t}")
        print(f"         所要 平均 {a['ms_mean']:.1f}ms  中央 {a['ms_p50']:.1f}ms  p95 {a['ms_p95']:.1f}ms  最大 {a['ms_max']:.1f}ms")
        print("         文字数別 " + "  ".join(f"{lo}〜{hi - 1 if hi < 10**9 else ''}字 {v['n']}件 中央 {v['p50']:.1f}ms p95 {v['p95']:.1f}ms"
                                         for (lo, hi), v in a["by_len"].items()))
        for romaji, kana, r in a["fail_clean"]:
            print(f"  [きれい失敗] {romaji}\n      正解 {kana}\n      1位  {r.zenz_input() if r else None}  {r.breakdown if r else ''}")
        for typo, notes, kana, r, k in a["fail_noisy"]:
            print(f"  [打鍵ミス失敗 順位={k}] {typo}  {notes}\n      正解 {kana}\n      1位  {r.zenz_input() if r else None}  {r.breakdown if r else ''}")
    m = run_misfix()
    print(f"user_style（typo 以外 {m['n']}文）  誤補正 {m['misfix']}/{m['n']}")
    for romaji, r in m["fails"][:args.fails]:
        print(f"  [誤補正] {romaji}\n      1位  {r.zenz_input()}  {r.breakdown}")
    w = run_raw(args.n, args.fails)
    print(f"raw_tokens（{w['by']['all']['n']}文）")
    for cat, b in w["by"].items():
        print(f"  {cat:9s} 1位 {b['top1']}/{b['n']}  上位5 {b['top5']}/{b['n']}")
    for romaji, ref, r, k in w["fails"]:
        print(f"  [失敗 順位={k}] {romaji}\n      正解 {ref}\n      1位  {r.zenz_input() if r else None}  {r.breakdown if r else ''}")
    u = run_user(args.n, args.fails)
    print("user_style_reading（40文）")
    for cat, b in u["by"].items():
        en = f"  英語区間が上位5 {b['en_top5']}/{b['has_en']}" if b["has_en"] else ""
        print(f"  {cat:9s} 1位 {b['top1']}/{b['n']}  上位5 {b['top5']}/{b['n']}{en}  誤補正 {b['misfix']}")
    for romaji, ref, r, k in u["fails"]:
        print(f"  [失敗 順位={k}] {romaji}\n      正解 {ref}\n      1位  {r.zenz_input() if r else None}  {r.breakdown if r else ''}")


if __name__ == "__main__":
    main()
