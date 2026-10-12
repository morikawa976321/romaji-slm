"""英単語の辞書（読みラティスの英語の弧）。

wordfreq（データ CC BY-SA 4.0）の英語上位語と、自作の技術語リスト core/data/tech_words.tsv を合わせる。
打鍵ミス1つ分の揺れは SymSpell と同じ「1字削除の索引」で引く（全変種を索引にするとメモリと構築時間が10倍以上になる）。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .keyboard import NEIGHBORS

TECH_PATH = Path(__file__).resolve().parent / "data" / "tech_words.tsv"

# 2文字以下で単独の英語区間にしてよい語（それ以外の2文字以下はローマ字との衝突が多すぎる）
SHORT_OK = frozenset({"ok", "ai", "ui", "ux", "os", "pr", "ci", "id", "pc", "ip", "uv"})
TOP_N = 25000  # 単独の英語区間にする wordfreq の上位語数
FUZZY_TOP_N = 10000  # 編集を通して引く語（短い語・低頻度語を通すと、日本語のローマ字が英単語の打ち間違いに見える）
FUZZY_MIN_LEN = 4


@dataclass(frozen=True)
class EnWord:
    spelling: str
    zipf: float  # wordfreq の頻度（10 億語あたりの出現数の log10 + 9）。技術語で wordfreq に無いものは 0
    tech: bool
    surfaces: tuple[str, ...]  # 表記の候補（登録表記、続けて綴りのまま）


@dataclass(frozen=True)
class Fuzzy:
    word: EnWord
    kind: str  # Edit.kind と同じ
    offset: int  # 打った部分文字列の中での位置
    detail: str


class Lexicon:
    def __init__(self, words: dict[str, EnWord], fuzzy_words: set[str]):
        self.words = words
        self.maxlen = max(map(len, words))
        self.deletes: dict[str, list[tuple[str, int]]] = {}
        for w in fuzzy_words:
            for k in range(len(w)):
                self.deletes.setdefault(w[:k] + w[k + 1:], []).append((w, k))
        self.fuzzy_words = fuzzy_words

    def get(self, s: str) -> EnWord | None:
        return self.words.get(s)

    def fuzzy(self, x: str) -> list[Fuzzy]:
        """打った x を1編集で辞書語にする候補。x 自身が辞書語でも探す（呼ぶ側で区別する）。"""
        out: list[Fuzzy] = []
        fw = self.fuzzy_words
        # 脱字（打った x は w から1字欠けている）
        for w, k in self.deletes.get(x, ()):
            out.append(Fuzzy(self.words[w], "drop", k, "+" + w[k]))
        n = len(x)
        for k in range(n):
            rest = x[:k] + x[k + 1:]
            # 余計な1字（同じ字の重ね打ちか、隣のキー）
            if rest in fw:
                c = x[k]
                prev = x[k - 1] if k else ""
                nxt = x[k + 1] if k + 1 < n else ""
                if c in (prev, nxt) or (prev and c in NEIGHBORS.get(prev, ())) or (nxt and c in NEIGHBORS.get(nxt, ())):
                    out.append(Fuzzy(self.words[rest], "insert", k, "-" + c))
            # 隣接キーへの置換（同じ位置を1字削った形が一致する語のうち、その位置の字が隣接キー）
            for w, j in self.deletes.get(rest, ()):
                if j == k and len(w) == n and w[k] in NEIGHBORS.get(x[k], ()):
                    out.append(Fuzzy(self.words[w], "sub", k, f"{x[k]}>{w[k]}"))
            # 隣接2字の入れ替え
            if k + 1 < n and x[k] != x[k + 1]:
                sw = x[:k] + x[k + 1] + x[k] + x[k + 2:]
                if sw in fw:
                    out.append(Fuzzy(self.words[sw], "swap", k, f"{x[k:k + 2]}>{sw[k:k + 2]}"))
        return out


def load_tech(path: Path = TECH_PATH) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        spelling, _, surface = line.partition("\t")
        out.setdefault(spelling.strip(), surface.strip() or spelling.strip())
    return out


def _wordfreq_top(n: int) -> list[tuple[str, float]]:
    try:
        from wordfreq import top_n_list, zipf_frequency
    except ImportError:  # 依存が無くても技術語だけで動かす
        return []
    out = []
    for w in top_n_list("en", n):
        if w.isascii() and w.isalpha() and w.islower():
            out.append((w, zipf_frequency(w, "en")))
    return out


@lru_cache(maxsize=1)
def lexicon() -> Lexicon:
    words: dict[str, EnWord] = {}
    fuzzy: set[str] = set()
    for rank, (w, z) in enumerate(_wordfreq_top(TOP_N)):
        words[w] = EnWord(w, z, False, (w,))
        if rank < FUZZY_TOP_N and len(w) >= FUZZY_MIN_LEN:
            fuzzy.add(w)
    try:
        from wordfreq import zipf_frequency
    except ImportError:
        zipf_frequency = None
    for spelling, surface in load_tech().items():
        z = words[spelling].zipf if spelling in words else (zipf_frequency(spelling, "en") if zipf_frequency else 0.0)
        surfaces = (surface,) if surface == spelling else (surface, spelling)
        words[spelling] = EnWord(spelling, z, True, surfaces)
        if len(spelling) >= 3 and spelling.isalpha():
            fuzzy.add(spelling)
    return Lexicon(words, fuzzy)
