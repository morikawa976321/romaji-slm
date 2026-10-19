"""常駐入力の打鍵から変換までの待ちを、本物の変換の時間で模擬する（docs/SPEEDUP.md §4 の B・C の効き）。

    uv run python -m eval.ime_sim [--threads 4] [--profiles fast,normal] [--policies base,B,C,BC] [--lc 60]

常駐入力（ime/Composer.cs）の要求の出し方を、仮想の時計の上で再現する。

- 打鍵が DEBOUNCE ms 止まったら、今の打鍵列を変換に出す。送るのは 50 ms ごとの Tick（ime/RomajiSlmIme.cs の timer）。
- base: 要求は同時に 1 つだけ（今と同じ）。走っている要求が終わるまで、新しい打鍵列は出さない。
- C（古い先読みの打ち切り）: 走っている要求より新しい打鍵列が出せるようになったら、古い要求をやめて新しい方を出す。
  やめられるのは forward の切れ目（走っている forward が終わるまで待つ）。
- B（別の書き方を最初の応答から外す）: 要求は読みの候補だけで返す（1 位は同じ）。別の書き方は候補ウィンドウを
  開いたときに計算する（その時間は別に測って示す）。

変換は本物（ONNX Runtime 版、core.ort_backend）を 1 要求ずつ流し、その壁時計の時間を仮想の時計に足す。
打鍵の間隔は対数正規分布（中央値 = 設定、σ 0.4）と、ときどきの間（10% の打鍵の後に 300〜1000 ms）。
同じ文には、どの方式にも同じ打鍵の時刻を使う。前置きの KV の取り置きは方式ごとに分ける。

測るのは「最後の打鍵から、全部打った打鍵列の 1 位が出るまで」。速度は測った機械と条件を添えて記録する（CLAUDE.md）。
"""

import argparse
import json
import math
import random
import statistics
import sys
import time
from collections import OrderedDict
from pathlib import Path

from .datasets import load_set
from .report import env_info

OUT = Path(__file__).resolve().parent / "out"
DEBOUNCE = 120  # ms（ime/Composer.cs の DebounceMs）
TICK = 50       # ms（ime/RomajiSlmIme.cs の timer.Interval）
PROFILES = {"fast": 130, "normal": 220, "slow": 330}  # 打鍵の間隔の中央値 ms


def key_times(n: int, median_ms: float, rng: random.Random) -> list[float]:
    t, out = 0.0, []
    for i in range(n):
        if i:
            t += median_ms * math.exp(rng.gauss(0, 0.4))
            if rng.random() < 0.10:
                t += rng.uniform(300, 1000)
        out.append(t)
    return out


def _tick(t: float) -> float:
    return math.ceil(t / TICK) * TICK


class Runner:
    """1 要求を本物の変換で流し、時間と forward の区切り（要求の始まりからの ms）を返す。"""

    def __init__(self, p):
        from core import window
        self.p, self.window = p, window
        be = p.backend
        self.spans: list[tuple[float, float]] = []
        self._t0 = 0.0
        orig = be._run

        def run(*a, **k):
            s = time.perf_counter()
            r = orig(*a, **k)
            self.spans.append(((s - self._t0) * 1000, (time.perf_counter() - self._t0) * 1000))
            return r

        be._run = run

    def __call__(self, romaji: str, lc: str, first_only: bool) -> tuple[float, list[tuple[float, float]]]:
        self.spans = []
        self._t0 = time.perf_counter()
        if first_only:
            pl = self.window.pool(self.p, romaji, lc, alts=False)
            if pl is not None:
                self.window.arrange(pl, self.window.DEFAULT_ORDER, 6, self.window.EDIT_SLOTS)
        else:
            self.p.convert_nbest(romaji, lc)
        return (time.perf_counter() - self._t0) * 1000, self.spans


def simulate(run: Runner, romaji: str, lc: str, times: list[float], policy: str) -> dict:
    cancel, first_only = "C" in policy, "B" in policy
    n = len(romaji)
    done: dict[str, float] = {}
    inflight = None  # (打鍵列, 始まり, 終わり, forward の区切り)
    t, i = 0.0, 0     # 仮想の時刻、次の打鍵
    n_req = n_abort = 0
    while romaji not in done:
        last_key = times[i - 1] if i else -1e9
        buf = romaji[:i]
        # 次に起きること: 打鍵、要求の終わり、送れるようになる Tick
        cands = []
        if i < n:
            cands.append(times[i])
        if inflight:
            cands.append(inflight[2])
        want = buf and buf not in done and (inflight is None or (cancel and inflight[0] != buf))
        if want:
            cands.append(max(_tick(last_key + DEBOUNCE), _tick(t)))
        t_next = min(cands)
        t = max(t, t_next)
        if inflight and inflight[2] <= t:
            done.setdefault(inflight[0], inflight[2])
            inflight = None
            continue
        if i < n and times[i] <= t:
            i += 1
            continue
        if want and t >= _tick(last_key + DEBOUNCE):
            start = t
            if inflight:  # C: 走っている forward が終わったところでやめる
                b0, s0, e0, spans = inflight
                rel = t - s0
                cut = next((e for s, e in spans if s <= rel < e), rel)
                start = max(t, s0 + cut)
                inflight = None
                n_abort += 1
            ms, spans = run(buf, lc, first_only)
            inflight = (buf, start, start + ms, spans)
            n_req += 1
    return {"latency": done[romaji] - times[-1], "n_req": n_req, "n_abort": n_abort}


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--set", default="user_style")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=30)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--profiles", default="fast,normal")
    ap.add_argument("--policies", default="base,B,C,BC")
    ap.add_argument("--lc", type=int, default=60, help="左文脈の字数（直前の文の正解をつなぐ。常駐入力は最大 60 字送る）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args(argv)

    from core.ort_backend import OrtCorePipeline
    items = [it for it in load_set(args.set) if args.min_chars <= len(it.refs[0]) <= args.max_chars][:args.limit]
    p = OrtCorePipeline(threads=args.threads)
    run = Runner(p)
    for it in items[:2]:
        p.convert_nbest(it.romaji, "")
    profiles = args.profiles.split(",")
    policies = args.policies.split(",")
    caches = {pol: OrderedDict() for pol in policies}
    rows = []
    single = {"full": [], "first": [], "alt": []}
    prev = items[-1].refs[0] if items else ""  # 1 文目にも左文脈を付ける
    for k, it in enumerate(items):
        lc = prev[-args.lc:] if args.lc else ""
        # 1 要求の時間（全部打った打鍵列、前置きの取り置きなし）: 今の応答と、B の最初の応答
        for _ in range(2):
            p.backend.cache = OrderedDict()
            full, _s = run(it.romaji, lc, False)
            p.backend.cache = OrderedDict()
            first, _s = run(it.romaji, lc, True)
        single["full"].append(full)
        single["first"].append(first)
        single["alt"].append(full - first)
        for prof in profiles:
            times = key_times(len(it.romaji), PROFILES[prof], random.Random(f"{args.seed}-{k}-{prof}"))
            for pol in policies:
                p.backend.cache = caches[pol]
                r = simulate(run, it.romaji, lc, times, pol)
                rows.append({"id": it.id, "profile": prof, "policy": pol, "keys": len(it.romaji),
                             "typing_ms": times[-1], **r})
        prev = (prev + it.refs[0])[-60:]
        print(f"{k + 1}/{len(items)}", file=sys.stderr, flush=True)

    env = env_info("cpu")
    md = render(rows, single, env, args, len(items))
    print(md)
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"ime_sim-{args.tag or 'run'}-{time.strftime('%Y%m%d-%H%M%S')}"
    (OUT / f"{name}.md").write_text(md, encoding="utf-8")
    (OUT / f"{name}.json").write_text(json.dumps({"env": env, "args": vars(args), "rows": rows, "single": single},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def render(rows, single, env, args, n_items) -> str:
    med = statistics.median
    lines = [f"# 打鍵から変換までの待ち（{time.strftime('%Y-%m-%d %H:%M')}）", "",
             f"- 機械: {env.get('cpu')}（論理 {env.get('logical_cpus')}）、ONNX Runtime 版、スレッド {args.threads}",
             f"- 入力: {args.set} の正解 {args.min_chars}〜{args.max_chars} 字、{n_items} 文、左文脈 {args.lc} 字",
             f"- 打鍵の間隔の中央値: " + "、".join(f"{p} {PROFILES[p]} ms" for p in args.profiles.split(",")),
             "", "1 要求の時間（全部打った打鍵列、中央値 ms）: "
             f"今の応答 {med(single['full']):.0f}、B の最初の応答 {med(single['first']):.0f}、"
             f"B で候補ウィンドウを開いたときに足す別の書き方 {med(single['alt']):.0f}",
             "", "| 打鍵 | 方式 | 待ちの中央値 ms | p90 | 最大 | 1 秒を超えた文 | 要求/文 | 打ち切り/文 |", "|---|---|---|---|---|---|---|---|"]
    for prof in args.profiles.split(","):
        for pol in args.policies.split(","):
            rs = [r for r in rows if r["profile"] == prof and r["policy"] == pol]
            lat = [r["latency"] for r in rs]
            lines.append(f"| {prof} | {pol} | {med(lat):.0f} | {_pct(lat, 0.9):.0f} | {max(lat):.0f} | "
                         f"{sum(x > 1000 for x in lat)}/{len(lat)} | {statistics.mean(r['n_req'] for r in rs):.1f} | "
                         f"{statistics.mean(r['n_abort'] for r in rs):.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
