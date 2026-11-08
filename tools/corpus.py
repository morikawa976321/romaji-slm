"""本人が書いた文（語彙の表の材料）を集め、AI が誤字脱字を直した版を作る道具。方針と手順は docs/CORPUS.md。
直すかどうかの判断は AI が行い（docs/CORPUS.md「直しの決まり」）、この道具は集める・束に分ける・直しを当てるだけを受け持つ。

    uv run python -m tools.corpus collect     # sources.json の出どころから raw.jsonl を作り直す
    uv run python -m tools.corpus prepare     # まだ直していない文を束に分けて fix/in/<束>.jsonl に書く
    uv run python -m tools.corpus apply       # fix/out/<束>.json の直しを当てて clean.jsonl を作る
    uv run python -m tools.corpus stats       # 出どころごとの件数と字数（文の中身は出さない）

置き場所は %LOCALAPPDATA%\\romaji-slm\\corpus\\（本人の文と本人のファイルの場所なので、リポジトリに入れない）:
    sources.json         出どころの一覧（書式は collect_* の各関数）
    raw.jsonl            集めた文 {"id", "source", "date", "text"}。1 行は発言 1 つか段落 1 つ
    fix/in/<束>.jsonl     直す係に渡す束 {"id", "text"}
    fix/out/<束>.json     直す係が返す {"fixes": [{"id", "from", "to"}], "drop": [id]}
    clean.jsonl          直した文。fix_log.jsonl は当てた直しと当たらなかった直し
"""

import argparse
import collections
import datetime as dt
import glob
import hashlib
import importlib.util
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

BASE = Path(os.environ.get("ROMAJI_SLM_DATA") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "romaji-slm")
CORPUS = BASE / "corpus"
SOURCES = CORPUS / "sources.json"
RAW = CORPUS / "raw.jsonl"
FIX_IN = CORPUS / "fix" / "in"
FIX_OUT = CORPUS / "fix" / "out"
CLEAN = CORPUS / "clean.jsonl"
FIX_LOG = CORPUS / "fix_log.jsonl"

JP = re.compile(r"[぀-ヿ一-鿿]")
# 定時ジョブ・自動の親セッションが送った指示文で始まる会話（本人の文ではない）。ops の各工程のプロンプトの書き出し
AUTO_FIRST = re.compile(r"^(taskboxをdrainして|毎日04:00の定時ジョブ|romaji-slm の利用履歴にマーカー以降|docs/parent-session-prompt\.md を読み)")
# アプリやハーネスが本人の発言の枠に入れる定型文（スキルの本文、要約からの再開、タスクカード、割り込みの印など）
GENERATED = re.compile(r"^\s*(<|This session is being continued|Base directory for this skill|Another Claude session sent|\[SYSTEM"
                       r"|\[Request interrupted|Caveat:|このタスクをここで実行してください)")
PASTE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)
CODE = re.compile(r"```.*?```", re.S)
# 1,000 字を超えて見出し・表・箇条が多い発言は、貼り付けた文書（AI の報告や外の資料）とみなす（2026-11-08 の実測で 24 件・約 3 万字）
DOC_JP, DOC_LINES = 1000, 5
UNIT_MAX = 600       # 段落がこれより長ければ行で分ける（直しの id を細かくするため）
FILE_MIN_JP = 100    # これより日本語の少ないファイルは英語の課題やデータとみなして捨てる
BATCH_CHARS = 6000   # 1 束の字数の目安（直す係が 1 回で読む量）


def jp_len(s: str) -> int:
    return len(JP.findall(s))


def unit_id(source: str, text: str) -> str:
    # 中身から決める。集め直しても同じ文は同じ id になり、済んだ直しがそのまま当たる
    return hashlib.sha1(f"{source}\n{text}".encode("utf-8")).hexdigest()[:16]


def split_units(text: str) -> list[str]:
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= UNIT_MAX:
            out.append(para)
        else:
            out += [ln.strip() for ln in para.splitlines() if ln.strip()]
    return out


def clean_message(text: str) -> str:
    return CODE.sub("", PASTE.sub("", text)).strip()


def looks_like_document(text: str) -> bool:
    lines = text.splitlines()
    marks = sum(ln.lstrip().startswith(("#", "|", "- ", "* ")) for ln in lines)
    return jp_len(text) > DOC_JP and marks > DOC_LINES


def collect_claude_archive(src: dict) -> list[tuple[str, str]]:
    """ops の archive-transcripts が残す会話（1 セッション 1 ファイル、1 行目が見出し、以降 {ts, role, text}）の本人の発言。"""
    out = []
    for f in sorted(glob.glob(os.path.join(src["path"], "*", "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh][1:]
        users = [r for r in rows if r.get("role") == "user"]
        if not users or AUTO_FIRST.match((users[0].get("text") or "").strip()):
            continue
        for r in users:
            t = r.get("text") or ""
            if GENERATED.match(t):
                continue
            t = clean_message(t)
            if jp_len(t) < 2 or looks_like_document(t):
                continue
            out.append(((r.get("ts") or "")[:10], t))
    return out


def collect_md_quotes(src: dict) -> list[tuple[str, str]]:
    """md の `>` の引用行（context-engine では本人の原文）。日付は frontmatter の created。"""
    out = []
    for pat in src["globs"]:
        for f in sorted(glob.glob(os.path.join(src["root"], pat), recursive=True)):
            text = Path(f).read_text(encoding="utf-8")
            m = re.search(r"^created:\s*(\S+)", text, re.M)
            date = m.group(1) if m else ""
            for ln in text.splitlines():
                if ln.startswith(">"):
                    q = ln[1:].strip()
                    if jp_len(q) >= 2:
                        out.append((date, q))
    return out


def docx_text(path: str) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = ["".join(t.text or "" for t in p.iter(ns + "t")) for p in root.iter(ns + "p")]
    return "\n\n".join(p for p in paras if p.strip())


def collect_files(src: dict) -> list[tuple[str, str]]:
    """.txt・.md・.docx。globs で拾い、exclude（相対パスへの正規表現）で外す。files は個別の指定。日付は mtime（写した日のこともある）。"""
    root = src["root"]
    paths = [os.path.join(root, f) for f in src.get("files", [])]
    for pat in src.get("globs", []):
        paths += glob.glob(os.path.join(root, pat), recursive=True)
    ex = re.compile(src["exclude"]) if src.get("exclude") else None
    out = []
    for p in sorted(set(paths)):
        rel = os.path.relpath(p, root)
        if ex and ex.search(rel):
            continue
        try:
            text = docx_text(p) if p.lower().endswith(".docx") else Path(p).read_text(encoding="utf-8", errors="replace")
        except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as e:
            print(f"skip {rel}: {type(e).__name__}", file=sys.stderr)
            continue
        if jp_len(text) < FILE_MIN_JP:
            continue
        date = dt.date.fromtimestamp(os.path.getmtime(p)).isoformat()
        out += [(date, u) for u in split_units(text)]
    return out


def collect_dialogue(src: dict) -> list[tuple[str, str]]:
    """**［話し手］** の行で区切った対話ログから、label の話し手の分だけ。"""
    text = Path(src["path"]).read_text(encoding="utf-8")
    out, speaker, buf = [], None, []
    for ln in text.splitlines() + ["**［end］**"]:
        m = re.match(r"^\*\*［(.+?)］\*\*\s*$", ln.strip())
        if m:
            if speaker == src["label"]:
                out += split_units("\n".join(buf))
            speaker, buf = m.group(1), []
        else:
            buf.append(ln)
    return [(src.get("date", ""), u) for u in out]


def collect_monocore(src: dict) -> list[tuple[str, str]]:
    """monocore（Supabase）の日次メモ・inbox と、AI が一度も書いていない予定の題。読むだけ。"""
    spec = importlib.util.spec_from_file_location("monocore_cli", src["module"])
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    cfg = m.load_config()
    env = m.get_env_config(cfg, "remote")
    headers = {"apikey": env["service_key"], "Authorization": f"Bearer {env['service_key']}"}
    uid = m.q(cfg["user_id"])

    def fetch(table, select):
        rows, off = [], 0
        while True:
            url = f"{env['url']}/rest/v1/{table}?user_id=eq.{uid}&select={select}&limit=1000&offset={off}"
            got = m.make_request(url, "GET", headers) or []
            rows += got
            if len(got) < 1000:
                return rows
            off += 1000

    out = []
    for r in fetch("schedule_day_notes", "day_key,body"):
        out += [(r["day_key"], u) for u in split_units(r.get("body") or "")]
    for r in fetch("inboxes", "content"):
        out += [("", ln.strip()) for ln in (r.get("content") or "").splitlines() if ln.strip()]
    writers = collections.defaultdict(set)
    for r in fetch("schedules", "activity,updated_by"):
        writers[(r.get("activity") or "").strip()].add(r.get("updated_by"))
    out += [("", a) for a, w in sorted(writers.items()) if a and "agent" not in w]
    return [(d, t) for d, t in out if jp_len(t) >= 1]


COLLECTORS = {"claude_archive": collect_claude_archive, "md_quotes": collect_md_quotes, "files": collect_files,
              "dialogue": collect_dialogue, "monocore": collect_monocore}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def cmd_collect(args) -> None:
    cfg = json.loads(SOURCES.read_text(encoding="utf-8-sig"))
    rows, seen, firsts = [], set(), []
    for src in cfg["sources"]:
        got = COLLECTORS[src["type"]](src)
        kept = 0
        for date, text in got:
            if jp_len(text) < 2:   # 表の数字・英文の段落は語彙の材料にならない
                continue
            key = re.sub(r"\s+", "", text)
            # 同じ文の重複（会話のフォーク、同じ引用）と、先の出どころに含まれる引用（engine の原文は会話から写したもの）を落とす
            if key in seen or (src.get("dedupe_against_previous") and any(key in f for f in firsts)):
                continue
            seen.add(key)
            rows.append({"id": unit_id(src["name"], text), "source": src["name"], "date": date, "text": text})
            kept += 1
        if src["type"] == "claude_archive":
            firsts.append("\n".join(re.sub(r"\s+", "", r["text"]) for r in rows if r["source"] == src["name"]))
        print(f"{src['name']}: {kept} 件（拾った {len(got)} 件）", file=sys.stderr)
    write_jsonl(RAW, rows)
    cmd_stats(args)


def done_ids() -> set[str]:
    ids = set()
    for f in FIX_IN.glob("*.jsonl"):
        if (FIX_OUT / (f.stem + ".json")).exists():
            ids |= {r["id"] for r in read_jsonl(f)}
    return ids


def cmd_prepare(args) -> None:
    """直していない文を束に分ける。出した束で、まだ返事の無いものは作り直す（中身が変わっていれば名前も変わる）。"""
    for f in FIX_IN.glob("*.jsonl"):
        if not (FIX_OUT / (f.stem + ".json")).exists():
            f.unlink()
    done = done_ids()
    todo = [r for r in read_jsonl(RAW) if r["id"] not in done]
    batches, cur, size = [], [], 0
    for r in todo:
        cur.append({"id": r["id"], "text": r["text"]})
        size += len(r["text"])
        if size >= BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
    if cur:
        batches.append(cur)
    for b in batches:
        name = hashlib.sha1("".join(r["id"] for r in b).encode()).hexdigest()[:12]
        write_jsonl(FIX_IN / f"{name}.jsonl", b)
    print(f"束 {len(batches)} 個（文 {len(todo)} 件、済み {len(done)} 件）: {FIX_IN}")


def apply_fixes(text: str, fixes: list[dict]) -> tuple[str, int, int]:
    """from を text の最初の出現で to に置き換える。見つからない直しは当てない（直す係の写し間違い）。"""
    hit = miss = 0
    for fx in fixes:
        a, b = fx.get("from") or "", fx.get("to") or ""
        if a and a != b and a in text:
            text = text.replace(a, b, 1)
            hit += 1
        elif a != b:
            miss += 1
    return text, hit, miss


def cmd_apply(args) -> None:
    fixes, drops, answered = collections.defaultdict(list), set(), set()
    for f in FIX_OUT.glob("*.json"):
        out = json.loads(f.read_text(encoding="utf-8"))
        batch = {r["id"]: r["text"] for r in read_jsonl(FIX_IN / (f.stem + ".jsonl"))}
        for fx in out.get("fixes", []):
            fid = fx.get("id")
            if fx.get("from") and fx["from"] not in batch.get(fid, ""):
                # 直す係が id を隣の文と取り違えることがある（2026-11-08 の試しで 8 個中 1 個）。
                # 同じ束で from を含む文が 1 つだけなら、その文の直しとみなす
                owners = [i for i, t in batch.items() if fx["from"] in t]
                if len(owners) == 1:
                    fid = owners[0]
            fixes[fid].append(dict(fx, id=fid))
        drops |= set(out.get("drop", []))
        answered |= set(batch)
    clean, log = [], []
    n_hit = n_miss = 0
    for r in read_jsonl(RAW):
        if r["id"] in drops:
            log.append({"id": r["id"], "drop": True})
            continue
        text, hit, miss = apply_fixes(r["text"], fixes.get(r["id"], []))
        n_hit, n_miss = n_hit + hit, n_miss + miss
        if hit or miss:
            log.append({"id": r["id"], "fixes": fixes[r["id"]], "hit": hit, "miss": miss})
        clean.append(dict(r, text=text, fixed=hit, checked=r["id"] in answered))
    write_jsonl(CLEAN, clean)
    write_jsonl(FIX_LOG, log)
    checked = sum(r["checked"] for r in clean)
    print(f"直した文 {sum(1 for r in clean if r['fixed'])} 件・直し {n_hit} 個（当たらなかった {n_miss} 個）・捨てた文 {len(drops)} 件。"
          f"確かめ済み {checked}/{len(clean)} 件 → {CLEAN}")


def cmd_stats(args) -> None:
    rows = read_jsonl(CLEAN) or read_jsonl(RAW)
    by = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        by[r["source"]][0] += 1
        by[r["source"]][1] += jp_len(r["text"])
    for k, (n, c) in by.items():
        print(f"  {k}: {n} 件・日本語 {c:,} 字")
    print(f"  計: {sum(v[0] for v in by.values())} 件・日本語 {sum(v[1] for v in by.values()):,} 字")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "prepare", "apply", "stats"):
        sub.add_parser(name)
    args = ap.parse_args()
    {"collect": cmd_collect, "prepare": cmd_prepare, "apply": cmd_apply, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
