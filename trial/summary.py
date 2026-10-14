"""結果ブロック: 本人がコピーして返すテキスト（docs/TRIAL.md「結果ブロック」）。

冒頭に本人の明示回答を置き、比較の記録は補助として後ろに並べる。集計は今回のセッションの記録だけ。
記録（records.jsonl）は押すたびに1行増える。同じ id の行は最後のものが最終の状態。
"""

from datetime import datetime

PUNCT_KINDS = (("。", "句点"), ("、", "読点"), ("？", "疑問符"))


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


def merge_client(server: list[dict], client: list[dict]) -> list[dict]:
    """サーバに保存済みの記録に、ブラウザにだけ残っている新しい版（rev が大きい）を重ねる。重ねた行は _unsaved。"""
    merged = {r["id"]: r for r in latest_by_id(server) if r.get("id")}
    order = list(merged)
    for c in latest_by_id(client):
        cid = c.get("id")
        if not cid:
            continue
        s = merged.get(cid)
        if s is None or (c.get("rev") or 0) > (s.get("rev") or 0):
            if s is None:
                order.append(cid)
            merged[cid] = {**c, "_unsaved": True}
    return [merged[k] for k in order]


def answers_of(records: list[dict]) -> dict[str, str]:
    return {r["question"]: r.get("value") for r in records if r.get("kind") == "answer" and r.get("question")}


def option(configs: dict, question: str, value) -> dict | None:
    return next((o for o in configs["questions"][question]["options"] if o["value"] == value), None)


def combo_id(configs: dict, answers: dict, base: str) -> str:
    """回答 → 体感と計測に使う組合せの設定 id `<model>/<punct>/<en_case>`。未回答は暫定の選択肢（最後の選択肢）の値。"""
    parts = [base]
    for key in ("punct", "en_case"):
        opts = configs["questions"][key]["options"]
        o = option(configs, key, answers.get(key)) or opts[-1]
        parts.append(o[key])
    return "/".join(parts)


def with_base(cid: str, base: str) -> str:
    return "/".join([base, *cid.split("/")[1:]])


def combo_label(cid: str) -> str:
    parts = cid.split("/")
    if len(parts) != 3:
        return cid
    return f"{parts[0]} + punct={parts[1]} + en_case={parts[2]}"


def _ms(x) -> str:
    return "?" if x is None else str(round(x))


def _launch_line(launch: list[dict], session: str) -> str:
    def fmt(e: dict) -> str:
        s = f"serve {e['serve_s']:.1f}s" if e.get("serve_s") is not None else "serve ?"
        if e.get("ready_s") is not None:
            s += f" / ready {e['ready_s']:.1f}s"
        elif e.get("error"):
            s += f" / ready 失敗（{e['error']}）"
        return s

    first = next((e for e in launch if e.get("first")), None)
    now = next((e for e in reversed(launch) if e.get("session") == session), None)
    if first:
        line = f"初回起動: {fmt(first)}"
    elif launch:
        line = f"初回起動: 不明（初回の記録なし。最初の記録 {fmt(launch[0])}）"
    else:
        line = "初回起動: 不明（run.cmd から起動していない）"
    if now is not None and now is not first:
        line += f" ／ 今回の起動: {fmt(now)}"
    return line


def _answer_line(configs: dict, question: str, answers: dict) -> str:
    q = configs["questions"][question]
    value = answers.get(question)
    o = option(configs, question, value)
    if o is None:
        text = "未回答"
        if q.get("provisional"):
            text += f"（暫定 {q['provisional']}）"
    else:
        text = o["short"]
        if o.get("provisional") and q.get("provisional"):
            text += f"（暫定 {q['provisional']}）"
    return f"【回答】{q['title']}: {text}"


def _ms_list(rows: list[dict]) -> str:
    return ", ".join(_ms(r.get("ms")) + ("(失敗)" if r.get("error") else "") for r in rows)


def _latest_measures(measures: list[dict], phase: str, cid: str) -> list[dict]:
    by_index: dict[int, dict] = {}
    for m in measures:
        if m.get("phase") == phase and m.get("config_id") == cid:
            by_index[m.get("index", 0)] = m
    return [by_index[i] for i in sorted(by_index)]


def _measure_line(measures: list[dict], final: str, speed_base: str) -> str:
    rows = _latest_measures(measures, "final", final)
    if rows:
        ms = [r["ms"] for r in rows if r.get("ms") is not None]
        part1 = (f"{combo_label(final)} {len(rows)}文: 各 {_ms_list(rows)} ms, 最大 {_ms(max(ms) if ms else None)}ms, "
                 f"1秒超 {sum(1 for x in ms if x > 1000)}件")
        failed = sum(1 for r in rows if r.get("error"))
        if failed:
            part1 += f", 失敗 {failed}件"
    else:
        part1 = f"{combo_label(final)}: 未実行"
    fast = with_base(final, speed_base)
    a, b = _latest_measures(measures, "speed", final), _latest_measures(measures, "speed", fast)
    if a or b:
        part2 = (f"{final.split('/')[0]} vs {speed_base} {max(len(a), len(b))}文: "
                 f"{final.split('/')[0]} {_ms_list(a) or '未実行'} ms / {speed_base} {_ms_list(b) or '未実行'} ms")
    else:
        part2 = f"{final.split('/')[0]} vs {speed_base}: 未実行"
    return f"計測: {part1} ／ {part2}"


def _out(card: dict | None, ms: bool = False) -> str:
    if card is None:
        return "-"
    text = f"(失敗:{card.get('text', '')})" if card.get("error") else card.get("text", "")
    return f"{text} {_ms(card.get('ms'))}ms" if ms else text


def _inserted(a: dict | None, b: dict | None) -> str:
    """句読点 B の出力が A に比べて足した記号。"""
    if not a or not b or a.get("error") or b.get("error"):
        return "不明（失敗あり）"
    kinds = [name for ch, name in PUNCT_KINDS if b.get("text", "").count(ch) > a.get("text", "").count(ch)]
    return "/".join(kinds) if kinds else "挿入なし"


def _choice(configs: dict, step: dict, r: dict, reasons: dict) -> str:
    ch = r.get("choice")
    two = len(step["configs"]) == 2
    if ch is None:
        return "未回答"
    if ch == "all_ok":
        return "どちらも問題ない" if two else "どれも問題ない"
    if ch == "neither":
        why = reasons.get(r.get("reason"), r.get("reason") or "理由未選択")
        return f"{'どちらも' if two else 'どれも'}使えない({why})"
    return step["names"].get(ch, ch)


def _compare_line(configs: dict, step_id: str, r: dict, total: int) -> str:
    step = configs["steps"][step_id]
    reasons = {x["value"]: x["label"] for x in configs.get("reasons", [])}
    by_cid = {c.get("config_id"): c for c in r.get("cards", [])}
    outs = [f"{step['names'].get(c, c)}({c})={_out(by_cid.get(c), ms=True)}" for c in step["configs"]]
    texts = {by_cid[c].get("text") for c in step["configs"] if c in by_cid}
    parts = [f"{step['title']} {r.get('index', 0) + 1}/{total} \"{r.get('input', '')}\" " + " | ".join(outs)]
    if r.get("cards"):
        parts.append("同一出力" if len(texts) == 1 else "差あり")
    else:
        parts.append("未変換")
    if step_id == "punct" and r.get("cards"):
        a, b = step["configs"][0], step["configs"][1]
        parts.append(f"{step['names'].get(b, b)}: {_inserted(by_cid.get(a), by_cid.get(b))}")
    parts.append(f"回答: {_choice(configs, step, r, reasons)}")
    return " | ".join(parts) + (" *未保存" if r.get("_unsaved") else "")


def _feel_line(n: int, r: dict) -> str:
    ctx = len(r.get("left_context") or "")
    src = {"example1": "例1", "example2": "例2（続き）", "own": "自分の文", "speed": "速度優先で同じ文"}.get(
        r.get("source"), r.get("source") or "")
    return (f"{n}. [{combo_label(r.get('config_id', '?'))}] \"{r.get('input', '')}\" → {_out(r)} "
            f"{_ms(r.get('ms'))}ms 文脈={ctx}字（{src}）" + (" *未保存" if r.get("_unsaved") else ""))


def render(*, version: str, session: str, machine: dict, engine: str, configs: dict, records: list[dict],
           client_records: list[dict], measures: list[dict], launch: list[dict], past: list[dict],
           now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    recs = merge_client(records, client_records)
    answers = answers_of(recs)
    final = combo_id(configs, answers, configs["feel_base"])
    feel = sorted((r for r in recs if r.get("kind") == "feel"), key=lambda r: r.get("seq", 0))
    felt = list(dict.fromkeys(r.get("config_id") for r in feel if r.get("source") != "speed"))
    tried_speed = any(r.get("source") == "speed" for r in feel)
    mem = machine.get("memory_gb")
    unsaved = sum(1 for r in recs if r.get("_unsaved"))

    lines = [
        f"romaji-slm trial {version} / {now.strftime('%Y-%m-%d %H:%M')} / session {session}",
        f"機種: {machine.get('cpu')} / {machine.get('logical_cores')} / {mem if mem is not None else '?'}GB / "
        f"{machine.get('os')} / Python {machine.get('python')} / torch {machine.get('torch') or 'なし'} / エンジン {engine}",
        _launch_line(launch, session),
        _answer_line(configs, "punct", answers),
        _answer_line(configs, "en_case", answers),
        _answer_line(configs, "use", answers),
        f"体感した設定: {' → '.join(combo_label(c) for c in felt) if felt else 'なし'}"
        f"（{configs['speed_base']} も試した: {'はい' if tried_speed else 'いいえ'}）",
        _measure_line(measures, final, configs["speed_base"]),
    ]
    past_sessions = {r.get("session") or "(旧版)" for r in past}
    if past_sessions:
        lines.append(f"過去の記録: {len(past_sessions)} セッション・{len(past)} 行（今回の集計には含めない）")
    if unsaved:
        lines.append(f"未保存の記録: {unsaved} 件（サーバに保存できず、ブラウザにだけ残っていたもの。行末に *未保存）")
    lines.append("--- 比較の記録 ---")
    for step_id in configs["steps"]:
        rows = sorted((r for r in recs if r.get("kind") == "compare" and r.get("step") == step_id),
                      key=lambda r: r.get("index", 0))
        total = max([len(rows)] + [r.get("total", 0) for r in rows])
        lines += [_compare_line(configs, step_id, r, total) for r in rows]
    lines.append("--- 体感の記録 ---")
    lines += [_feel_line(i, r) for i, r in enumerate(feel, 1)] or ["なし"]
    return "\n".join(lines) + "\n"
