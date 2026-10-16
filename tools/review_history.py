"""利用履歴の振り返りの道具（docs/REVIEW.md）。判断（どれが誤変換か、何を辞書に足すか）は AI が行い、この道具は
読み出し・マーカー・辞書の合流だけを受け持つ。

    uv run python -m tools.review_history count                  # マーカー以降の確定の件数（ops の起動判定に使う）
    uv run python -m tools.review_history extract                # マーカー以降の行を作業フォルダに書き出す
    uv run python -m tools.review_history merge <提案.tsv>        # 提案した辞書の行を user_dict.tsv に足す（控えを残す）
    uv run python -m tools.review_history mark <時刻>             # マーカーを <時刻>（extract が出した until）に進め、
                                                                 # 振り返り済みの行と作業の残りを消す（下の「消すもの」）

置き場所はすべて %LOCALAPPDATA%\\romaji-slm\\（本人が打った文を含むのでリポジトリには入れない）:
    history.jsonl         常駐入力の記録（ime/RomajiSlmIme.cs の History）
    review-state.json     マーカー {"reviewed_until": 最後に振り返った行の time, "runs": [...]}
    review/<日付>/         作業フォルダ（new.jsonl・提案・評価用の組）
    review/real_local.jsonl  振り返りで正解を付けた本人の誤変換の組（評価セット。回を重ねて足し、90 日で消える）
    user_dict.tsv         ユーザー辞書（core/userdict.py）

消すもの（本人の決定 2026-10-17「1-4をいれて」: 入力が全部残るのはリスクなので、振り返りが済んだら消す）:
    - history.jsonl の、マーカー以前の行（mark のとき）
    - review/real_local.jsonl の、source が REAL_KEEP_DAYS 日より古い組（mark のとき）
    - review/<日付>/ の作業フォルダと、リポジトリの eval/out の real_local の評価結果（mark のとき）
    - 30 日より古い history.jsonl の行は、振り返られなくても常駐アプリが消す（ime/RomajiSlmIme.cs の History）
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

BASE = Path(os.environ.get("ROMAJI_SLM_DATA") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "romaji-slm")
HISTORY = BASE / "history.jsonl"
STATE = BASE / "review-state.json"
REVIEW = BASE / "review"
USER_DICT = BASE / "user_dict.tsv"
REAL_KEEP_DAYS = 90
REPO = Path(__file__).resolve().parent.parent


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"reviewed_until": "", "runs": []}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def new_rows(since: str) -> list[dict]:
    if not HISTORY.exists():
        return []
    rows = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("time", "") > since:
            rows.append(r)
    return sorted(rows, key=lambda r: r.get("time", ""))   # 消すときの付け直しで順が前後することがある


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def flag(rows: list[dict]) -> None:
    """誤変換の手がかりを各行に付ける（AI が読む順番の目安。判断ではない）。"""
    for i, r in enumerate(rows):
        f = []
        if r.get("event") == "cancel":
            f.append("cancel")
        if r.get("event") == "restore":
            f.append("restore")
        if r.get("chosen", 0) > 0:
            f.append("chose_other")
        if r.get("retype"):
            f.append("retype")
        if i + 1 < len(rows):
            n = rows[i + 1]
            a, b = r.get("romaji") or "", n.get("romaji") or ""
            same_app = r.get("app") == n.get("app")
            if same_app and a and b and a != b and _edit_distance(a, b) <= max(2, len(a) // 5):
                f.append("retyped_next")   # すぐ後に似た打鍵列を打ち直した（denatu → dennnatu）
            if same_app and a and a == b and r.get("event") == "commit":
                f.append("same_romaji_next")
        r["flags"] = f


def cmd_count(args) -> int:
    st = load_state()
    rows = [r for r in new_rows(st["reviewed_until"]) if r.get("event") == "commit"]
    print(len(rows))
    return 0


def cmd_extract(args) -> int:
    st = load_state()
    rows = new_rows(st["reviewed_until"])
    flag(rows)
    day = dt.date.today().isoformat()
    out = REVIEW / day
    out.mkdir(parents=True, exist_ok=True)
    (out / "new.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    until = max((r.get("time", "") for r in rows), default=st["reviewed_until"])
    flagged = sum(bool(r["flags"]) for r in rows)
    print(json.dumps({"dir": str(out), "rows": len(rows), "commits": sum(r.get("event") == "commit" for r in rows),
                      "flagged": flagged, "since": st["reviewed_until"], "until": until}, ensure_ascii=False))
    return 0


def cmd_merge(args) -> int:
    from core.userdict import parse
    proposed = Path(args.proposed).read_text(encoding="utf-8")
    d = parse(proposed)
    if d.errors:
        print("読めない行:", *d.errors, sep="\n  ")
        return 1
    existing = USER_DICT.read_text(encoding="utf-8") if USER_DICT.exists() else "# 本人のユーザー辞書（romaji-slm の docs/REVIEW.md）\n"
    if USER_DICT.exists():
        shutil.copy2(USER_DICT, USER_DICT.with_name(f"user_dict.{dt.datetime.now():%Y%m%d-%H%M%S}.bak"))
    have = {tuple(line.split("\t")[:3]) for line in existing.splitlines() if line and not line.startswith("#")}
    added = []
    for line in proposed.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if tuple(line.split("\t")[:3]) not in have:
            added.append(line)
    USER_DICT.write_text(existing.rstrip("\n") + "\n" + "".join(a + "\n" for a in added), encoding="utf-8")
    print(json.dumps({"added": len(added), "path": str(USER_DICT)}, ensure_ascii=False))
    return 0


def cmd_mark(args) -> int:
    st = load_state()
    if args.until < st["reviewed_until"]:
        print("マーカーは戻さない")
        return 1
    st["reviewed_until"] = args.until
    st["runs"].append({"at": dt.datetime.now().isoformat(timespec="seconds"), "until": args.until, "note": args.note or ""})
    save_state(st)
    removed = prune_history(args.until)
    old = prune_real_local()
    dirs = clean_work()
    print(json.dumps({"reviewed_until": args.until, "history_removed": removed, "real_local_expired": old,
                      "work_removed": dirs}, ensure_ascii=False))
    return 0


def prune_history(until: str) -> int:
    """マーカー以前の行を history.jsonl から消す。常駐アプリが同時に書き足しても行を失わないよう、先に名前を替えて
    （次の書き足しは新しい history.jsonl に行く）から、残す行を追記で戻す。"""
    if not HISTORY.exists():
        return 0
    tmp = HISTORY.with_name(f"history.pruning-{os.getpid()}.jsonl")
    os.replace(HISTORY, tmp)
    keep, removed = [], 0
    for line in tmp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            t = json.loads(line).get("time", "")
        except json.JSONDecodeError:
            removed += 1
            continue
        if t > until:
            keep.append(line)
        else:
            removed += 1
    if keep:
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write("".join(k + "\n" for k in keep))
    tmp.unlink()
    return removed


def prune_real_local() -> int:
    p = REVIEW / "real_local.jsonl"
    if not p.exists():
        return 0
    limit = (dt.date.today() - dt.timedelta(days=REAL_KEEP_DAYS)).isoformat()
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    keep = [r for r in rows if (r.get("meta") or {}).get("source", "9999") >= limit]
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    return len(rows) - len(keep)


def clean_work() -> int:
    n = 0
    if REVIEW.exists():
        for d in REVIEW.iterdir():
            if d.is_dir():
                shutil.rmtree(d)
                n += 1
    out = REPO / "eval" / "out"
    if out.exists():
        for f in out.glob("*real_local*"):
            f.unlink()
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("count")
    sub.add_parser("extract")
    m = sub.add_parser("merge")
    m.add_argument("proposed")
    k = sub.add_parser("mark")
    k.add_argument("until")
    k.add_argument("--note")
    args = ap.parse_args(argv)
    return {"count": cmd_count, "extract": cmd_extract, "merge": cmd_merge, "mark": cmd_mark}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
