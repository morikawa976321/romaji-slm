"""本人の文（tools/corpus.py が作る clean.jsonl）から、読みと表記の組の回数を数えた語彙の表を作る。

    uv run --group data python -m tools.vocab

出力は %LOCALAPPDATA%\\romaji-slm\\corpus\\vocab.tsv（本人の語彙なのでリポジトリに入れない）。
列と数え方は docs/CORPUS.md「語彙の表」。
"""

import collections
import json
import re
import unicodedata

from data.reading import _JP_CHARS, _KATA, _split_alnum, _tokenizer
from tools.corpus import CLEAN, CORPUS, read_jsonl

VOCAB = CORPUS / "vocab.tsv"
# この日から本人の文に romaji-slm 自身の出力が混ざる（常駐入力 D19）。誤変換を本人の語として強めていないか
# 見分けられるよう、これより前の回数を別の列に持つ
ROMAJI_SLM_FROM = "2026-10-16"
_KANA = re.compile(r"^[ぁ-ゖァ-ヺー]+$")
FIELDS = ("reading", "surface", "lemma", "pos", "unit", "count", "units", "before")


def to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def to_kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def _morphs(text: str, mode) -> list[tuple[int, int, str, str | None, str, str]]:
    """(始まり, 終わり, 表記, ひらがなの読み, 品詞, 正規形) の列。読みは表記どおり（助詞は は・を のまま。打つ綴りと同じ）で、
    分からない語は None。正規形は SudachiDict の表記揺れの寄せ先（すべて・全て → 全て）で、同じ語の書き分けを束ねるのに使う。

    結果はその場で取り出す。SudachiPy 0.7 の Morpheme は解析器の入力を共有していて、Morpheme.split を続けて呼ぶと
    別の文の位置を読んで panic した（2026-11-08）。だから split を使わず、単位ごとに解析し直して位置で突き合わせる。
    """
    tok, _ = _tokenizer()
    out = []
    for m in tok.tokenize(text, mode):
        s, r = m.surface(), m.reading_form()
        if _KATA.match(r):
            r = to_hira(r)
        elif m.is_oov() and _KANA.match(s):  # 辞書に無いかなの語は表記が読み
            r = to_hira(s)
        else:
            r = None
        out.append((m.begin(), m.end(), s, r, "-".join(p for p in m.part_of_speech()[:2] if p != "*"), m.normalized_form()))
    return out


def words(text: str) -> tuple[list[tuple[str, str, str, str, str]], list[str]]:
    """文を (読み, 表記, 正規形, 品詞, 単位) の列と、読みの分からない語の列にする。

    単位は SudachiPy の長い単位（C）と短い単位（A）。両方で同じ区切りの語は "AC" として 1 回、
    C が A に分かれる語は C の語と A の各語をそれぞれ数える。
    英数字の連続は数えない。数字の直後の語（助数詞）も数えない — SudachiDict の読みが数字抜きでは決まらない（data/reading.py）。
    """
    from sudachipy import tokenizer

    out, unknown = [], []
    prev_digits = False
    for is_lit, chunk in _split_alnum(text):
        if is_lit:
            prev_digits = unicodedata.normalize("NFKC", chunk).isdigit()
            continue
        c = _morphs(chunk, tokenizer.Tokenizer.SplitMode.C)
        a = _morphs(chunk, tokenizer.Tokenizer.SplitMode.A)
        if prev_digits and c:
            cut = c[0][1]
            c, a = c[1:], [m for m in a if m[0] >= cut]
        prev_digits = False
        spans_c, spans_a = {m[:2] for m in c}, {m[:2] for m in a}
        units = [(m, "AC" if m[:2] in spans_a else "C") for m in c] + [(m, "A") for m in a if m[:2] not in spans_c]
        for (_, _, s, r, pos, lemma), unit in sorted(units, key=lambda u: u[0][:2]):
            if not _JP_CHARS.match(s):
                continue
            if r is None:
                unknown.append(s)
                continue
            out.append((r, s, lemma, pos, unit))
    return out, unknown


def count(rows: list[dict]) -> tuple[dict, collections.Counter]:
    """{(読み, 表記, 正規形, 品詞, 単位): [回数, 出た文の数, ROMAJI_SLM_FROM より前の回数]} と、読みの分からない語の回数。"""
    table: dict = collections.defaultdict(lambda: [0, 0, 0])
    unknown: collections.Counter = collections.Counter()
    for r in rows:
        ws, unk = words(r["text"])
        unknown.update(unk)
        before = (r.get("date") or "") < ROMAJI_SLM_FROM
        for key, n in collections.Counter(ws).items():
            v = table[key]
            v[0] += n
            v[1] += 1
            v[2] += n if before else 0
    return table, unknown


def add_variants(table: dict, dic=None) -> int:
    """本人が使った語（同じ読み・同じ正規形の組）ごとに、Mozc の辞書にある同じ語の別の書き方を回数 0 の行（単位 "V"）として足す。
    本人が一度も使っていない書き方（「総て」）も、本人の書き方より下げる対象にするため（core.uservocab）。足した行の数を返す。

    同じ語かどうかは、Mozc がその読みに載せている表記を SudachiPy にかけて 1 語になり、正規形が一致するかで決める。
    かなの書き方（読みのひらがな・カタカナ）も同じように確かめる。読みは比べない — 表記だけを解析すると SudachiPy は
    一つの読みに決めてしまい（良い → ヨイ）、Mozc が「いい」に載せている 良い を落とすため。"""
    from sudachipy import tokenizer

    from core.kanakanji import Dictionary

    dic = dic or Dictionary.load()
    tok, _ = _tokenizer()
    mode = tokenizer.Tokenizer.SplitMode.C
    groups: dict = collections.defaultdict(dict)   # (読み, 正規形) → {表記: 品詞}
    for (r, s, lemma, pos, _), (n, _, _) in table.items():
        if n:
            groups[(r, lemma)].setdefault(s, pos)
    memo: dict = {}

    def as_word(t: str):
        if t not in memo:
            ms = list(tok.tokenize(t, mode))
            memo[t] = ms[0].normalized_form() if len(ms) == 1 else None
        return memo[t]

    added = 0
    for (r, lemma), surfaces in groups.items():
        k = to_kata(r)
        # 本人の表と同じく日本語の文字だけの表記に限る（英数字は数えていないので、「１」を いち の使わない書き方にしない）
        cands = {t for t in {e[3] for e in dic.lookup(k)} | {r, k} if _JP_CHARS.match(t)}
        pos = next(iter(surfaces.values()))
        for t in sorted(cands - set(surfaces)):
            if as_word(t) == lemma:
                table[(r, t, lemma, pos, "V")] = [0, 0, 0]
                added += 1
    return added


def write_table(path, table: dict) -> None:
    items = sorted(table.items(), key=lambda kv: (-kv[1][0], kv[0]))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\t".join(FIELDS) + "\n")
        for (r, s, lemma, pos, unit), (n, u, b) in items:
            fh.write(f"{r}\t{s}\t{lemma}\t{pos}\t{unit}\t{n}\t{u}\t{b}\n")


HOLDOUT_SET = CORPUS / "holdout.jsonl"
HOLDOUT_VOCAB = CORPUS / "vocab_train.tsv"
_SENT = re.compile(r"(?<=[。！？!?])|\n")


def is_held(unit_id: str, k: int) -> bool:
    return int(unit_id[:8], 16) % k == 0


def holdout_items(rows: list[dict], limit: int) -> list[dict]:
    """取り分けた文を、本人の文体の合成セット（eval/build_synth.py）と同じ道具で評価の項目にする。
    打鍵列は SudachiPy の読みから作り（表記揺れの乱数つき）、句読点は文のとおりに打つ。正解は本人が書いた表記。"""
    import random

    from data.build import _HAS_WORD, _TYPED_OK, segment_pieces
    from data.reading import Unreadable, analyze
    from data.romaji import UnreadableKana, romanize
    from data.symbols import is_typable
    from tools.corpus import jp_len

    out, seen = [], set()
    for r in rows:
        for s in _SENT.split(r["text"]):
            s = s.strip()
            n = jp_len(s)
            if s in seen or not 6 <= n <= 40 or n < 0.6 * len(s) or "http" in s or "`" in s:
                continue
            seen.add(s)
            try:
                romaji = romanize(segment_pieces(analyze(s)), random.Random(f"0:{s}"))
            except (Unreadable, UnreadableKana):
                continue
            if not romaji or not _TYPED_OK.match(romaji) or not is_typable(romaji) or not _HAS_WORD.search(romaji):
                continue
            out.append({"romaji": romaji, "output": s, "left_context": "",
                        "meta": {"category": "user_holdout", "source": r["source"], "date": r.get("date", "")}})
    # 並びは中身から決める（評価の id は行の番号なので、作り直しても同じ項目が同じ id になるように）
    import hashlib
    out.sort(key=lambda it: hashlib.sha1(it["output"].encode()).hexdigest())
    return out[:limit]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--holdout", type=int, default=0, metavar="K",
                    help="文の K 件に 1 件を取り分け、残りで vocab_train.tsv を、取り分けた文で評価セット holdout.jsonl を作る")
    ap.add_argument("--limit", type=int, default=600, help="評価セットの項目の上限")
    args = ap.parse_args()
    rows = read_jsonl(CLEAN)
    if not rows:
        raise SystemExit(f"{CLEAN} が無い。先に tools.corpus apply を走らせる")
    if args.holdout:
        train = [r for r in rows if not is_held(r["id"], args.holdout)]
        held = [r for r in rows if is_held(r["id"], args.holdout)]
        train_table = count(train)[0]
        add_variants(train_table)
        write_table(HOLDOUT_VOCAB, train_table)
        items = holdout_items(held, args.limit)
        HOLDOUT_SET.write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
        print(f"取り分け {len(held)} 件（残り {len(train)} 件で {HOLDOUT_VOCAB}）→ 評価の項目 {len(items)} 件: {HOLDOUT_SET}")
        return
    table, unknown = count(rows)
    n_var = add_variants(table)
    write_table(VOCAB, table)
    (CORPUS / "vocab_unknown.json").write_text(
        json.dumps(unknown.most_common(), ensure_ascii=False), encoding="utf-8")

    pairs = collections.Counter()
    for (r, s, _, _, unit), (n, _, _) in table.items():
        if unit != "V":
            pairs[(r, s)] += n
    two = {k: n for k, n in pairs.items() if n >= 2}
    by_reading = collections.defaultdict(list)
    for (r, s), n in two.items():
        by_reading[r].append(n)
    print(f"文 {len(rows)} 件 → 読みと表記の組 {len(pairs):,} 個（2 回以上 {len(two):,} 個）・"
          f"語の延べ {sum(pairs.values()):,}。2 回以上の表記が 2 つ以上ある読み {sum(len(v) > 1 for v in by_reading.values()):,} 個。"
          f"読みの分からない語 {len(unknown):,} 種（延べ {sum(unknown.values()):,}）。"
          f"辞書から足した書き分け {n_var:,} 行 → {VOCAB}")


if __name__ == "__main__":
    main()
