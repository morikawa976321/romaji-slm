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
FIELDS = ("reading", "surface", "pos", "unit", "count", "units", "before")


def to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _morphs(text: str, mode) -> list[tuple[int, int, str, str | None, str]]:
    """(始まり, 終わり, 表記, ひらがなの読み, 品詞) の列。読みは表記どおり（助詞は は・を のまま。打つ綴りと同じ）で、分からない語は None。

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
        out.append((m.begin(), m.end(), s, r, "-".join(p for p in m.part_of_speech()[:2] if p != "*")))
    return out


def words(text: str) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """文を (読み, 表記, 品詞, 単位) の列と、読みの分からない語の列にする。

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
        for (_, _, s, r, pos), unit in sorted(units, key=lambda u: u[0][:2]):
            if not _JP_CHARS.match(s):
                continue
            if r is None:
                unknown.append(s)
                continue
            out.append((r, s, pos, unit))
    return out, unknown


def count(rows: list[dict]) -> tuple[dict, collections.Counter]:
    """{(読み, 表記, 品詞, 単位): [回数, 出た文の数, ROMAJI_SLM_FROM より前の回数]} と、読みの分からない語の回数。"""
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


def main() -> None:
    rows = read_jsonl(CLEAN)
    if not rows:
        raise SystemExit(f"{CLEAN} が無い。先に tools.corpus apply を走らせる")
    table, unknown = count(rows)
    items = sorted(table.items(), key=lambda kv: (-kv[1][0], kv[0]))
    with open(VOCAB, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\t".join(FIELDS) + "\n")
        for (r, s, pos, unit), (n, u, b) in items:
            fh.write(f"{r}\t{s}\t{pos}\t{unit}\t{n}\t{u}\t{b}\n")
    (CORPUS / "vocab_unknown.json").write_text(
        json.dumps(unknown.most_common(), ensure_ascii=False), encoding="utf-8")

    pairs = collections.Counter()
    for (r, s, _, _), (n, _, _) in table.items():
        pairs[(r, s)] += n
    two = {k: n for k, n in pairs.items() if n >= 2}
    by_reading = collections.defaultdict(list)
    for (r, s), n in two.items():
        by_reading[r].append(n)
    print(f"文 {len(rows)} 件 → 読みと表記の組 {len(pairs):,} 個（2 回以上 {len(two):,} 個）・"
          f"語の延べ {sum(pairs.values()):,}。2 回以上の表記が 2 つ以上ある読み {sum(len(v) > 1 for v in by_reading.values()):,} 個。"
          f"読みの分からない語 {len(unknown):,} 種（延べ {sum(unknown.values()):,}）→ {VOCAB}")


if __name__ == "__main__":
    main()
