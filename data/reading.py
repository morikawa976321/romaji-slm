"""出力文の形態素解析（SudachiPy）と、zenz データの読みとの照合。"""

import re
import unicodedata
import warnings
from dataclasses import dataclass
from functools import lru_cache

from data.symbols import to_typed

_KATA = re.compile(r"^[ァ-ヴー]+$")
_JP_CHARS = re.compile(r"^[ぁ-ゖァ-ヺーヽヾゝゞ一-鿿㐀-䶿々〆〇ヶヵ]+$")
_PARTICLE_ORTHO = {"は": "ワ", "へ": "エ", "を": "オ"}


class Unreadable(ValueError):
    """除外理由を reason に持つ。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class Token:
    surface: str
    kind: str  # ja | lit | sym | space
    typed: str = ""  # lit/sym: そのまま打つ文字列
    reading: str = ""  # ja: カタカナ読み（表記どおり。助詞「を」は ヲ）
    sound: str = ""  # ja: 発音どおりの読み（助詞「は」は ワ）。zenz の input との照合に使う


@lru_cache(maxsize=1)
def _tokenizer():
    from sudachipy import dictionary, tokenizer

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return dictionary.Dictionary().create(), tokenizer.Tokenizer.SplitMode.C


def _is_alnum(ch: str) -> bool:
    n = unicodedata.normalize("NFKC", ch)
    return n.isascii() and n.isalnum()


def _split_alnum(text: str) -> list[tuple[bool, str]]:
    """英数字の連続（全角含む）とそれ以外に分ける。"""
    runs: list[tuple[bool, str]] = []
    for ch in text:
        flag = _is_alnum(ch)
        if runs and runs[-1][0] == flag:
            runs[-1] = (flag, runs[-1][1] + ch)
        else:
            runs.append((flag, ch))
    return runs


_NUM_KANJI = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "七", "8": "八", "9": "九", "0": "十"}
_NUM_READ = {
    "一": ("イチ", "イッ", "イ"), "二": ("ニ",), "三": ("サン",), "四": ("ヨン", "ヨ", "シ"), "五": ("ゴ",),
    "六": ("ロク", "ロッ"), "七": ("ナナ", "シチ"), "八": ("ハチ", "ハッ", "ハ"), "九": ("キュウ", "ク"),
    "十": ("ジュウ", "ジュッ", "ジッ"),
}


def _analyze_ja(chunk: str, digits: str = "") -> list[Token]:
    """日本語の連続を解析する。直前が数字なら、助数詞の読み（年→ネン、分→プン）が出るよう漢数字を仮置きして解析する。"""
    tok, mode = _tokenizer()
    kanji = _NUM_KANJI.get(digits[-1:]) if digits.isascii() else None
    morphs = list(tok.tokenize((kanji or "") + chunk, mode))
    drop_first = None  # 仮置きした漢数字を落とした先頭トークンの (表記, 読み)
    if kanji:
        first = morphs[0]
        pre = None
        if first.surface().startswith(kanji) and first.surface() != kanji:
            reading = first.reading_form()
            pre = next((p for p in sorted(_NUM_READ[kanji], key=len, reverse=True) if reading.startswith(p)), None)
            if pre and len(reading) > len(pre):
                drop_first = (first.surface()[1:], reading[len(pre):])
        if first.surface() == kanji or drop_first:
            morphs = morphs[1:]
        else:
            morphs = list(tok.tokenize(chunk, mode))  # 助数詞の読みを取れなかった。仮置きなしで解析し直す
    out: list[Token] = []
    if drop_first:
        surface, reading = drop_first
        out.append(Token(surface, "ja", reading=reading, sound=reading))
    for m in morphs:
        s = m.surface()
        if s.strip() == "":
            out.append(Token(s, "space"))
            continue
        typed = to_typed(s)
        if typed is not None:
            out.append(Token(s, "sym", typed=typed))
            continue
        if not _JP_CHARS.match(s):
            raise Unreadable("unmapped_symbol", s)
        reading = m.reading_form()
        if not _KATA.match(reading):
            raise Unreadable("no_reading", f"{s}→{reading}")
        sound = reading
        if m.part_of_speech()[0] == "助詞" and s in _PARTICLE_ORTHO:
            sound = _PARTICLE_ORTHO[s]
        out.append(Token(s, "ja", reading=reading, sound=sound))
    return out


def analyze(text: str) -> list[Token]:
    """英数字の連続は解析にかけず、そのまま打つ綴りにする。

    SudachiDict は「1日」「3回」「10分」を1語にして読みも数字込みで返すため、
    英数字を先に切り出して、残りの日本語だけを解析する（数字の直後の助数詞は読みが文脈抜きになる）。
    """
    out: list[Token] = []
    prev_digits = ""
    for is_lit, chunk in _split_alnum(text):
        if is_lit:
            out.append(Token(chunk, "lit", typed=unicodedata.normalize("NFKC", chunk).lower()))
            n = unicodedata.normalize("NFKC", chunk)
            prev_digits = n if n.isdigit() else ""
            continue
        out.extend(_analyze_ja(chunk, prev_digits))
        prev_digits = ""
    return out


def _norm_kana(text: str) -> str:
    """照合用: カタカナと長音だけ残し、ヲ→オ・ヶ→ケなどをそろえる。"""
    out = []
    for ch in text:
        if "ぁ" <= ch <= "ゖ":
            ch = chr(ord(ch) + 0x60)
        ch = {"ヲ": "オ", "ヵ": "カ", "ヶ": "ケ"}.get(ch, ch)
        if "ァ" <= ch <= "ヴ" or ch == "ー":
            out.append(ch)
    return "".join(out)


def align_zenz(tokens: list[Token], zenz_input: str, max_subst: int = 2) -> tuple[list[Token], int] | None:
    """解析器の読みを zenz の input に合わせる。合えば (読みを確定した列, zenz の読みに差し替えた語数)、合わなければ None。

    漢字を含む語に限り、最大 max_subst 語まで読みを zenz の該当部分に差し替えてよい（私＝ワタクシ/ワタシのような揺れ）。
    英数字トークンは読みが不明なので任意の長さを許す。
    """
    z = _norm_kana(zenz_input)
    n = len(tokens)
    fail: set[tuple[int, int, int]] = set()

    def go(i: int, pos: int, budget: int) -> list[Token] | None:
        if i == n:
            return [] if pos == len(z) else None
        if (i, pos, budget) in fail:
            return None
        t = tokens[i]
        res: list[Token] | None = None
        if t.kind == "ja":
            cands = dict.fromkeys([_norm_kana(t.sound), _norm_kana(t.reading)])  # は→ワ/ハ どちらも通す
            for c in cands:
                if c and z.startswith(c, pos) and (rest := go(i + 1, pos + len(c), budget)) is not None:
                    res = [t, *rest]
                    break
            if res is None and budget and any(not ("ぁ" <= ch <= "ん" or "ァ" <= ch <= "ヴ" or ch == "ー") for ch in t.surface):
                for ln in range(1, min(len(z) - pos, len(t.reading) + 6) + 1):
                    if (rest := go(i + 1, pos + ln, budget - 1)) is not None:
                        sub = z[pos:pos + ln]
                        res = [Token(t.surface, "ja", reading=sub, sound=sub), *rest]
                        break
        elif t.kind == "lit":
            for ln in range(0, len(z) - pos + 1):
                if (rest := go(i + 1, pos + ln, budget)) is not None:
                    res = [t, *rest]
                    break
        else:
            res = go(i + 1, pos, budget)
            if res is not None:
                res = [t, *res]
        if res is None:
            fail.add((i, pos, budget))
        return res

    for budget in range(max_subst + 1):
        fail.clear()
        r = go(0, 0, budget)
        if r is not None:
            return r, sum(a.reading != b.reading for a, b in zip(tokens, r))
    return None
