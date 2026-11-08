"""本人の語彙の加点。本人が書いた文の「読み→表記」の回数（tools/vocab.py の vocab.tsv、docs/CORPUS.md）から、
変換で本人の書き方を少し優先する（本人の方針 2026-11-08、docs/decisions.md。モデルは追加学習しない）。

置き場所: 環境変数 ROMAJI_SLM_VOCAB、無ければ %LOCALAPPDATA%\\romaji-slm\\corpus\\vocab.tsv（無くてもよい）。

語の加点は、同じ読みの中で本人がその表記を使った割合: c(読み, 表記) / (c(読み) + PRIOR)。0〜1 に収まり、
変換の点数には重み w_voc を掛けて足す（core.constrained の貪欲法の選び方と、core.pipeline・core.window の点数）。
回数そのものではなく割合にするのは、語彙の表の雑音（造語の切れ端、変換し忘れのかな）が同じ読みの中で
1〜2% しか占めないため（docs/CORPUS.md「語彙の表」）。
"""

import collections
import os
from pathlib import Path

ENV = "ROMAJI_SLM_VOCAB"
MIN_COUNT = 2     # 1 回しか出ない組は使わない（誤字の取り残し・解析の誤りを拾いやすい）
MIN_READING = 2   # 読みが一字の語は使わない（「か→化」のように助詞と区切りを取り合う。docs/CORPUS.md）
PRIOR = 2         # 読みの回数に足す擬似回数。回数の少ない読みでは割合を割り引く（2 回中 2 回 → 0.5）


def _kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


class UserVocab:
    """(カタカナの読み, 表記) → 加点（0〜1）。ラティスの弧の読みはカタカナ（core.kanakanji）。"""

    def __init__(self, bonus: dict[tuple[str, str], float] | None = None):
        self.bonus = bonus or {}

    def get(self, reading: str, surface: str) -> float:
        return self.bonus.get((_kata(reading), surface), 0.0)

    def __len__(self) -> int:
        return len(self.bonus)

    def __bool__(self) -> bool:
        return bool(self.bonus)


def from_counts(counts: dict[tuple[str, str], int]) -> UserVocab:
    """{(読み, 表記): 回数} から。読みはひらがなかカタカナ。"""
    total: collections.Counter = collections.Counter()
    for (r, _), n in counts.items():
        total[_kata(r)] += n
    bonus = {}
    for (r, s), n in counts.items():
        k = _kata(r)
        if n >= MIN_COUNT and len(k) >= MIN_READING:
            bonus[(k, s)] = n / (total[k] + PRIOR)
    return UserVocab(bonus)


def default_path() -> Path:
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "romaji-slm" / "corpus" / "vocab.tsv"


def load(path: str | Path | None = None) -> UserVocab:
    """vocab.tsv を読む。無ければ空。同じ (読み, 表記) の行（品詞・単位違い）は回数を合わせる。"""
    p = Path(path) if path else default_path()
    if not p.exists():
        return UserVocab()
    counts: collections.Counter = collections.Counter()
    with open(p, encoding="utf-8") as fh:
        head = fh.readline().rstrip("\n").split("\t")
        ir, is_, ic = head.index("reading"), head.index("surface"), head.index("count")
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) > ic:
                counts[(f[ir], f[is_])] += int(f[ic])
    return from_counts(counts)
