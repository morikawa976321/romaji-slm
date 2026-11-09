r"""本人の語彙の書き分け。本人が書いた文の「読み→表記」の回数（tools/vocab.py の vocab.tsv、docs/CORPUS.md）から、
同じ語の書き方（すべて／全て／総て、ほしい／欲しい）を本人の書き方に寄せる（本人の方針 2026-11-08、docs/decisions.md D51。
モデルは追加学習しない）。

置き場所: 環境変数 ROMAJI_SLM_VOCAB、無ければ %LOCALAPPDATA%\romaji-slm\corpus\vocab.tsv（無くてもよい）。

同じ語 = 同じ読みで SudachiPy の正規形が同じ表記の組（表の lemma 列）。組の中で本人がその表記を使った割合
p(表記) = c / (組の回数 + PRIOR) を取り、点 = p(表記) − 組の最大の p（≤ 0）。本人がいちばん使う書き方は 0、
本人が使わない書き方ほど下がる。別の語（高官／交換、速い／早い）どうしには効かない — 読みだけで束ねて加点すると、
本人がよく使う語へ意味の違う語を寄せて壊した（D51）。重み w_voc を掛けて、core.constrained の貪欲法のトークンの選び方と、
core.window の 1 位と同じ読みの候補の並びに足す。読みどうしの比べ（core.pipeline）には入れない。
"""

import collections
import os
from pathlib import Path

ENV = "ROMAJI_SLM_VOCAB"
MIN_COUNT = 2     # 組でいちばん使う書き方が 1 回だけの組は使わない（誤字の取り残し・解析の誤りを拾いやすい）
MIN_READING = 2   # 読みが一字の語は使わない（「か→化」のように助詞と区切りを取り合う。docs/CORPUS.md）
PRIOR = 2         # 組の回数に足す擬似回数。回数の少ない組では差を割り引く（2 回対 0 回 → −0.5）


def _kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


class UserVocab:
    """(カタカナの読み, 表記) → 点（≤ 0。表に無い組は 0）。ラティスの弧の読みはカタカナ（core.kanakanji）。"""

    def __init__(self, bonus: dict[tuple[str, str], float] | None = None):
        self.bonus = bonus or {}

    def get(self, reading: str, surface: str) -> float:
        return self.bonus.get((_kata(reading), surface), 0.0)

    def __len__(self) -> int:
        return len(self.bonus)

    def __bool__(self) -> bool:
        return bool(self.bonus)


def from_counts(counts: dict[tuple[str, str, str], int]) -> UserVocab:
    """{(読み, 表記, 正規形): 回数} から。読みはひらがなかカタカナ。回数 0 の行は辞書にある本人の使わない書き方。

    一つの表記が同じ読みで複数の語に入るとき（いい = 良い・言い）は、点の高い方（減点の小さい方）を取る。
    ラティスの弧には正規形が無く、どちらの語として出たか決められないので、本人の書き方であるかもしれない方に倒す。"""
    groups: dict = collections.defaultdict(collections.Counter)
    for (r, s, lemma), n in counts.items():
        groups[(_kata(r), lemma)][s] += n
    score: dict[tuple[str, str], float] = {}
    for (k, _), c in groups.items():
        top = max(c.values())
        if len(k) < MIN_READING or top < MIN_COUNT:
            continue
        total = sum(c.values()) + PRIOR
        for s, n in c.items():
            v = (n - top) / total
            score[(k, s)] = max(score.get((k, s), v), v)
    return UserVocab({key: v for key, v in score.items() if v < 0})


def default_path() -> Path:
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "romaji-slm" / "corpus" / "vocab.tsv"


def load(path: str | Path | None = None) -> UserVocab:
    """vocab.tsv を読む。無い・lemma 列の無い古い表なら空。同じ (読み, 表記, 正規形) の行（品詞・単位違い）は回数を合わせる。"""
    p = Path(path) if path else default_path()
    if not p.exists():
        return UserVocab()
    counts: collections.Counter = collections.Counter()
    with open(p, encoding="utf-8") as fh:
        head = fh.readline().rstrip("\n").split("\t")
        if "lemma" not in head:
            return UserVocab()
        ir, is_, il, ic = (head.index(c) for c in ("reading", "surface", "lemma", "count"))
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) > max(ir, is_, il, ic):
                counts[(f[ir], f[is_], f[il])] += int(f[ic])
    return from_counts(counts)
