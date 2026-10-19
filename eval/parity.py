"""変換コアの2つの実行系（GPU の torch 版 core と CPU の ONNX Runtime 版 core-ort）の食い違いを1件ずつ調べる。

    # 1. 変換器ごとに、候補の内訳つきで流す（eval.run と同じ行に、候補 k 件の読み・出力・点数・完走を足す）
    uv run python -m eval.parity run --converter core --device cuda --set user_style --set gen_test --tag core
    uv run python -m eval.parity run --converter "core-ort:t=4" --device cpu --set user_style --tag ort
    # 2. 2つの結果を突き合わせ、食い違った件を原因の型に分ける
    uv run python -m eval.parity compare eval/out/parity/core__user_style.jsonl eval/out/parity/ort__user_style.jsonl

結果は eval/out/parity/<tag>__<set>.jsonl（eval.diff でもそのまま読める）。変換器と機械は <tag>__<set>.env.json に、
セットごとに書く（同じタグで設定の違う実行を混ぜても分かるように。compare は 2 つの変換器が違えば書き添える）。
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

from .converters import make_converter
from .datasets import load_set
from .metrics import english_kept, score
from .report import _TYPO_CATS, env_info

OUT = Path(__file__).resolve().parent / "out" / "parity"


def _num(x: float) -> float | None:
    return None if x is None or math.isinf(x) or math.isnan(x) else round(x, 5)


def run_one(conv, romaji: str, left: str) -> tuple[str, list[dict], bool, float]:
    t0 = time.perf_counter()
    cands = conv.candidates(romaji, left)
    out = conv._render_best(cands[0], romaji, left) if cands else ""
    ms = (time.perf_counter() - t0) * 1000
    rows = [{"reading": c["reading"], "edits": c["edits"], "text": c["text"], "finished": c["score"] != float("-inf"),
             "lm": _num(c["lm"]), "fluency": _num(c.get("fluency")), "read_cost": _num(c["read_cost"]),
             "score": _num(c["score"])} for c in cands]
    return out, rows, bool(cands and cands[0]["edits"]), ms


def cmd_run(args) -> None:
    conv = make_converter(args.converter, args.device, args.model)
    env = env_info(args.device)
    for _ in range(2):
        conv.convert("konnichiha")
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        for set_name in args.sets:
            items = load_set(set_name, args.limit)
            path = OUT / f"{args.tag}__{set_name.replace(':', '_').replace('/', '_')}.jsonl"
            t0 = time.perf_counter()
            with path.open("w", encoding="utf-8") as f:
                for it in items:
                    out, cands, edited, ms = run_one(conv, it.romaji, it.left_context)
                    refs = it.refs or [""]
                    row = {"id": it.id, "category": it.category, "romaji": it.romaji,
                           "left_context": it.left_context, "refs": it.refs, "output": out,
                           **(score(refs, out) if it.refs else {"exact": None}),
                           "en_kept": english_kept(refs[0], out) if it.refs else None,
                           "edited": edited, "ms": ms, "cands": cands}
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            # タグ 1 つに 1 ファイルだと、後の実行が前の実行の条件を上書きする（2026-10-18 の O0 は E セットを p=auto、
            # I セットを p=insert で回したのに、条件は p=insert だけが残った。docs/SPEEDUP.md §0）
            env_path(path).write_text(json.dumps({"converter": args.converter, "model": args.model, "set": set_name,
                                                  "env": env}, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{args.tag} {set_name}: {len(items)} 件 {time.perf_counter() - t0:.0f} s -> {path}", flush=True)
    finally:
        if hasattr(conv, "close"):
            conv.close()


def env_path(path: Path | str) -> Path:
    """結果の JSONL（<tag>__<set>.jsonl）に対応する条件のファイル。"""
    return Path(path).with_suffix(".env.json")


def converter_of(path: Path | str) -> str | None:
    """結果を出した変換器。条件のファイルが無い古い記録（2026-10-20 より前）は None。"""
    p = env_path(path)
    return json.loads(p.read_text(encoding="utf-8"))["converter"] if p.exists() else None


def load(path: Path | str) -> dict[str, dict]:
    return {(r := json.loads(l))["id"]: r for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()}


def _key(c: dict) -> tuple:
    # 同じ読みの文字列でも区間の分け方（英語の区間か、かなか）が違う候補がありうるので、読みのコストも鍵に入れる
    return c["reading"], tuple(c["edits"]), c["read_cost"]


def classify(a: dict, b: dict) -> dict:
    """出力が食い違った1件の原因の型。

    finish_b: B の1位の読みを A は未完で捨てた（行き止まり・探索の幅）。finish_a はその逆。
    decode: 同じ読みで、制約付き生成の出力の文が違う（探索・数値の差で貪欲法の選択が入れ替わった）。
    score: 候補の文はどれも同じで、点数（lm・自然さ）の差で1位が入れ替わった。
    punct: 1位の候補は同じで、句読点 B の挿入（自然さの数値）だけが違う。
    """
    ca = {_key(c): c for c in a["cands"]}
    cb = {_key(c): c for c in b["cands"]}
    wa, wb = a["cands"][0], b["cands"][0]
    info = {"win_a": wa, "win_b": wb}
    if _key(wa) == _key(wb):
        info["type"] = "decode" if wa["text"] != wb["text"] else "punct"
        return info
    xa, xb = ca.get(_key(wb)), cb.get(_key(wa))  # 相手の1位の読みを、こちらはどう扱ったか
    if xa is not None and not xa["finished"] and wb["finished"]:
        info["type"] = "finish_b"
        info["a_partial"] = xa["text"]
    elif xb is not None and not xb["finished"] and wa["finished"]:
        info["type"] = "finish_a"
        info["b_partial"] = xb["text"]
    elif (xa is not None and xa["text"] != wb["text"]) or (xb is not None and xb["text"] != wa["text"]):
        info["type"] = "decode"
    else:
        info["type"] = "score"
    # 同じ読み・同じ文の候補どうしの点数の差（数値の差の大きさ）
    d_lm, d_flu = [], []
    for k, x in ca.items():
        y = cb.get(k)
        if y and x["finished"] and y["finished"] and x["text"] == y["text"]:
            d_lm.append(y["lm"] - x["lm"])
            if x["fluency"] is not None and y["fluency"] is not None:
                d_flu.append(y["fluency"] - x["fluency"])
    info["max_d_lm"] = max(map(abs, d_lm), default=None)
    info["max_d_flu"] = max(map(abs, d_flu), default=None)
    return info


def compare(pa: Path | str, pb: Path | str) -> dict:
    A, B = load(pa), load(pb)
    ids = [i for i in A if i in B]
    rows = []
    for i in ids:
        a, b = A[i], B[i]
        if a["output"] == b["output"]:
            continue
        info = classify(a, b)
        clean = a["category"] not in _TYPO_CATS
        rows.append({"id": i, "category": a["category"], "clean": clean, "romaji": a["romaji"], "refs": a["refs"],
                     "out_a": a["output"], "out_b": b["output"], "ok_a": a.get("exact"), "ok_b": b.get("exact"),
                     **info})
    n = len(ids)
    acc = lambda R: sum(bool(R[i].get("exact")) for i in ids) / max(1, n)  # noqa: E731
    return {"n": n, "diff": rows, "acc_a": acc(A), "acc_b": acc(B), "conv_a": converter_of(pa), "conv_b": converter_of(pb),
            "b_breaks": [r for r in rows if r["clean"] and r["ok_a"] and not r["ok_b"]],
            "b_fixes": [r for r in rows if r["ok_b"] and not r["ok_a"]]}


def _set_files(tag: str) -> dict[str, Path]:
    return {p.name[len(tag) + 2:-len(".jsonl")]: p for p in sorted(OUT.glob(f"{tag}__*.jsonl"))}


def _reading_refs(set_file: str) -> dict[str, str]:
    """読み段のセット（typo_hard）の正解の読み。id は行番号（datasets.load_jsonl と同じ）。"""
    path = Path(set_file.replace("file_", "", 1).replace("_", "/", 2)) if set_file.startswith("file_") else None
    if path is None or not path.exists():
        return {}
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines]
    if not any("reading" in r for r in rows):  # 読み段のセット（トップレベルに reading を持つ）だけ
        return {}
    return {str(i): r.get("reading", "") for i, r in enumerate(rows)}


def table(tags: list[str], ref: str | None = None) -> str:
    """変種ごと・セットごとの精度。ref を渡すと、ref と出力が食い違った件数と、ref に対して正常入力で新たに壊れた件数も出す。"""
    sets = sorted({s for t in tags for s in _set_files(t)})
    lines = ["| セット | 変種 | 件数 | 完全一致 | ゆるい一致 | 正常入力の完全一致 | 誤補正 | 1位の読みが正解 | 食い違い（対 ref） "
             "| 正常入力で ref だけ正解 / 変種だけ正解 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for s in sets:
        R = load(_set_files(ref)[s]) if ref and s in _set_files(ref) else None
        rrefs = _reading_refs(s)
        for t in tags:
            f = _set_files(t).get(s)
            if f is None:
                continue
            rows = load(f)
            rs = list(rows.values())
            n = len(rs)
            if not n:
                continue
            has_ref = any(r["refs"] for r in rs)
            clean = [r for r in rs if r["category"] not in _TYPO_CATS]
            ex = f"{sum(bool(r['exact']) for r in rs) / n:.1%}" if has_ref else "—"
            lo = f"{sum(bool(r.get('loose')) for r in rs) / n:.1%}" if has_ref else "—"
            cex = f"{sum(bool(r['exact']) for r in clean) / max(1, len(clean)):.1%}" if has_ref and clean else "—"
            mis = f"{sum(bool(r['edited']) for r in clean) / max(1, len(clean)):.1%}" if clean else "—"
            rd = "—"
            if rrefs:
                rd = f"{sum(r['cands'][0]['reading'] == rrefs.get(i) for i, r in rows.items() if r['cands']) / n:.1%}"
            dv = br = "—"
            if R is not None and t != ref:
                ids = [i for i in rows if i in R]
                dv = str(sum(rows[i]["output"] != R[i]["output"] for i in ids))
                if has_ref:
                    cl = [i for i in ids if rows[i]["category"] not in _TYPO_CATS]
                    br = (f"{sum(bool(R[i]['exact']) and not rows[i]['exact'] for i in cl)} / "
                          f"{sum(bool(rows[i]['exact']) and not R[i]['exact'] for i in cl)}")
            lines.append(f"| {s} | {t} | {n} | {ex} | {lo} | {cex} | {mis} | {rd} | {dv} | {br} |")
    return "\n".join(lines) + "\n"


def trace(pairs: list[tuple[str, str]], sets: list[str] | None = None) -> str:
    """最初の組（例: T0 と L0）で出力が食い違った件が、後の組（片方だけを変えた変種の組）でも食い違うかの表。
    = は出力が一致、≠ は食い違い。○× は各組の B 側の正誤。"""
    a0, b0 = pairs[0]
    sets = sets or sorted(set(_set_files(a0)) & set(_set_files(b0)))
    head = " | ".join(f"{a}↔{b}" for a, b in pairs)
    lines = [f"| セット | id | カテゴリ | 正解 | {a0} | {b0} | {head} |",
             "|---|---|---|---|---|---|" + "---|" * len(pairs)]
    cache: dict[tuple[str, str], dict] = {}

    def get(tag, s):
        if (tag, s) not in cache:
            f = _set_files(tag).get(s)
            cache[(tag, s)] = load(f) if f else {}
        return cache[(tag, s)]

    for s in sets:
        A, B = get(a0, s), get(b0, s)
        for i in A:
            if i not in B or A[i]["output"] == B[i]["output"]:
                continue
            cells = []
            for a, b in pairs:
                x, y = get(a, s).get(i), get(b, s).get(i)
                if x is None or y is None:
                    cells.append("—")
                    continue
                ok = "" if y.get("exact") is None else ("○" if y["exact"] else "×")
                cells.append(("=" if x["output"] == y["output"] else "≠") + ok)
            mark = lambda r: "" if r.get("exact") is None else ("○" if r["exact"] else "×")  # noqa: E731
            lines.append(f"| {s} | {i} | {A[i]['category']} | {_c(A[i]['refs'][0] if A[i]['refs'] else '')} "
                         f"| {_c(A[i]['output'])}{mark(A[i])} | {_c(B[i]['output'])}{mark(B[i])} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _c(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render(res: dict, name_a: str, name_b: str) -> str:
    from collections import Counter
    rows = res["diff"]
    types = Counter(r["type"] for r in rows)
    ca, cb = res.get("conv_a"), res.get("conv_b")
    conv = f"- 変換器 A `{ca or '記録なし'}` / B `{cb or '記録なし'}`"
    if ca and cb and ca != cb:
        conv += "（**設定が違う**: 食い違いには設定の差も入る）"
    lines = [f"# 食い違い: {name_a}（A） vs {name_b}（B）", "", conv,
             f"- 件数 {res['n']}、出力が食い違った {len(rows)} 件（{', '.join(f'{k} {v}' for k, v in types.most_common())}）",
             f"- 完全一致 A {res['acc_a']:.1%} / B {res['acc_b']:.1%}",
             f"- 正常入力で A だけ正解（B が壊す）{len(res['b_breaks'])} 件、B だけ正解 {len(res['b_fixes'])} 件", "",
             "| id | カテゴリ | 型 | 正解 | A | B | A 正 | B 正 | 1位の読み A / B | 相手の1位を未完で捨てた文 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        part = r.get("a_partial", r.get("b_partial", ""))
        ra, rb = r["win_a"]["reading"], r["win_b"]["reading"]
        lines.append(f"| {r['id']} | {r['category']} | {r['type']} | {_c(r['refs'][0] if r['refs'] else '')} "
                     f"| {_c(r['out_a'])} | {_c(r['out_b'])} | {'○' if r['ok_a'] else '×'} | {'○' if r['ok_b'] else '×'} "
                     f"| {_c(ra)}{'' if ra == rb else ' / ' + _c(rb)} | {_c(part)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--converter", required=True)
    r.add_argument("--model")
    r.add_argument("--device", default="cuda", help="torch 版 core は cuda だけ。core-ort は CPU で動く（記録に書く装置）")
    r.add_argument("--set", action="append", required=True, dest="sets")
    r.add_argument("--tag", required=True, help="結果のファイル名の頭")
    r.add_argument("--limit", type=int)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--md", help="Markdown の保存先")
    c.add_argument("--json", help="仕分けの結果（JSON）の保存先")
    t = sub.add_parser("table")
    t.add_argument("tags", nargs="+")
    t.add_argument("--ref", help="食い違いを数える基準の変種")
    tr = sub.add_parser("trace")
    tr.add_argument("pairs", nargs="+", help="A:B の組。最初の組で食い違った件を後の組で追う")
    tr.add_argument("--set", action="append", dest="sets")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "table":
        print(table(args.tags, args.ref))
    elif args.cmd == "trace":
        print(trace([tuple(p.split(":")) for p in args.pairs], args.sets))
    else:
        res = compare(args.a, args.b)
        md = render(res, Path(args.a).stem, Path(args.b).stem)
        print(md)
        if args.md:
            Path(args.md).write_text(md, encoding="utf-8")
        if args.json:
            Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
