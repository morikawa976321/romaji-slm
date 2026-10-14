"""結果ブロック: 本人がコピーして返すテキスト（docs/TRIAL.md「結果ブロック」）。

記録（records.jsonl）は選択のたびに1行増える。同じ試行（id）の行は最後のものが最終の状態。
"""

import statistics
from datetime import datetime


def latest_by_id(records: list[dict]) -> list[dict]:
    """id ごとに最後の行を残し、最初に現れた順に並べる。"""
    order: list[str] = []
    last: dict[str, dict] = {}
    for i, r in enumerate(records):
        key = str(r.get("id") or f"_{i}")
        if key not in last:
            order.append(key)
        last[key] = r
    return [last[k] for k in order]


def percentile(xs: list[float], p: float) -> float | None:
    """最近接順位法の p パーセンタイル。"""
    if not xs:
        return None
    s = sorted(xs)
    i = max(0, min(len(s) - 1, -(-len(s) * p // 100) - 1))
    return s[int(i)]


def _ms(x) -> str:
    return "?" if x is None else str(round(x))


def _axis_short(axis: dict) -> str:
    return axis.get("short") or axis.get("label") or axis["id"]


def _launch_line(launch: list[dict]) -> str:
    if not launch:
        return "初回起動: 不明（run.cmd から起動していない）"

    def fmt(e: dict) -> str:
        s = f"応答 {e['serve_s']:.1f}秒" if e.get("serve_s") is not None else "応答 ?"
        if e.get("ready_s") is not None:
            s += f"・モデル読込完了 {e['ready_s']:.1f}秒"
        elif e.get("error"):
            s += f"・モデル読込失敗（{e['error']}）"
        return s

    first = next((e for e in launch if e.get("first")), None)
    line = f"初回起動: {fmt(first)}" if first else f"初回起動: 不明（初回の記録なし。最初の記録 {fmt(launch[0])}）"
    if launch[-1] is not first:
        line += f" / 最新の起動: {fmt(launch[-1])}"
    return line


def _bench_line(bench: list[dict], order: list[str]) -> str:
    latest: dict[str, dict] = {}
    for b in bench:
        latest[b["config_id"]] = b
    if not latest:
        return "常駐計測: 未実行"
    ids = [c for c in order if c in latest] + [c for c in latest if c not in order]
    parts = []
    for cid in ids:
        b = latest[cid]
        s = f"{cid} p50={_ms(b.get('p50'))} p95={_ms(b.get('p95'))} max={_ms(b.get('max'))} n={b.get('n', 0)}"
        if b.get("failed"):
            s += f" 失敗={b['failed']}"
        parts.append(s)
    return "常駐計測: " + "; ".join(parts)


def _card_cid(card: dict) -> str:
    return card.get("config_id", "?")


def _label_to_cid(r: dict) -> dict[str, str]:
    return {c.get("label"): _card_cid(c) for c in r.get("cards", [])}


def _axis_line(axis: dict, trials: list[dict]) -> str:
    cids = axis.get("configs", [])
    best = dict.fromkeys(cids, 0)
    fatal = dict.fromkeys(cids, 0)
    all_ok = 0
    for r in trials:
        l2c = _label_to_cid(r)
        if r.get("all_ok"):
            all_ok += 1
        if r.get("best") in l2c:
            c = l2c[r["best"]]
            best[c] = best.get(c, 0) + 1
        for lab in r.get("fatal") or []:
            if lab in l2c:
                c = l2c[lab]
                fatal[c] = fatal.get(c, 0) + 1
    fmt = lambda d: " ".join(f"{k}={v}" for k, v in d.items())  # noqa: E731
    return (f"[{_axis_short(axis)}] 入力 {len(trials)} 件: 一番 {fmt(best)}; 致命的 {fmt(fatal)}; "
            f"全部問題ない {all_ok}")


def _out(card: dict) -> str:
    if card.get("error"):
        return f"(失敗:{card.get('text', '')})"
    return card.get("text", "")


def _record_line(n: int, r: dict, axes: dict[str, dict]) -> str:
    if r.get("kind") == "single":
        verdict = {"used": "使えた", "fixed": "直した"}.get(r.get("verdict"), "-")
        ctx = f" 文脈={len(r.get('left_context', ''))}字" if r.get("left_context") else ""
        return (f"{n}. [打ってみる {r.get('config_id', '?')}] \"{r.get('input', '')}\" → {_out(r)} "
                f"{_ms(r.get('ms'))}ms{ctx} | {verdict}")
    axis = axes.get(r.get("axis"), {"id": r.get("axis", "?")})
    cards = " | ".join(f"{c.get('label')}({_card_cid(c)})={_out(c)} {_ms(c.get('ms'))}ms" for c in r.get("cards", []))
    if r.get("all_ok"):
        choice = "全部問題ない"
    else:
        choice = f"一番={r.get('best') or 'なし'} 致命的={','.join(r.get('fatal') or []) or 'なし'}"
    return f"{n}. [{_axis_short(axis)}] \"{r.get('input', '')}\" | {cards} | {choice}"


def render(version: str, machine: dict, configs: dict, records: list[dict], bench: list[dict],
           launch: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    axes = {a["id"]: a for a in configs.get("axes", [])}
    order = list(dict.fromkeys(c for a in configs.get("axes", []) for c in a.get("configs", [])))
    trials = latest_by_id(records)
    mem = machine.get("memory_gb")
    lines = [
        f"romaji-slm trial {version} / {now.strftime('%Y-%m-%d %H:%M')}",
        f"機種: {machine.get('cpu')} / {machine.get('logical_cores')} / {mem if mem is not None else '?'}GB / "
        f"{machine.get('os')} / Python {machine.get('python')} / torch {machine.get('torch') or 'なし'}",
        _launch_line(launch),
        _bench_line(bench, order),
    ]
    for axis in configs.get("axes", []):
        lines.append(_axis_line(axis, [r for r in trials if r.get("kind") == "compare" and r.get("axis") == axis["id"]]))
    singles = [r for r in trials if r.get("kind") == "single"]
    ms = [r["ms"] for r in singles if r.get("ms") is not None and not r.get("error")]
    lines.append(f"[打ってみる] {len(singles)} 件, 使えた {sum(r.get('verdict') == 'used' for r in singles)}, "
                 f"直した {sum(r.get('verdict') == 'fixed' for r in singles)}, "
                 f"中央値 {_ms(statistics.median(ms)) if ms else '?'}ms")
    lines.append("--- 記録 ---")
    shown = [r for r in trials if r.get("kind") in ("compare", "single")]
    lines += [_record_line(i, r, axes) for i, r in enumerate(shown, 1)]
    return "\n".join(lines) + "\n"
