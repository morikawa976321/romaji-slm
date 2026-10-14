"""打鍵列 → 読みの N-best（docs/REDESIGN.md 工程2「読みラティス」。仕組みとコストの根拠は docs/READING.md）。

打鍵列の文字位置を節点とし、次の弧を張って、コストの小さい順に経路を取り出す。

- 正規ローマ字の弧（コスト0）。`nn`+母音を「ン+な行」と読む弧は小さいコスト
- 打鍵ミスの編集弧（隣接キーへの置換・脱字・余計な1字・隣接2字の入れ替え）。1弧1編集、1文 max_edits まで
- 英語の弧（wordfreq の上位語＋core/data/tech_words.tsv）、辞書に無い英字列（略語・型番）の弧、数字の弧、記号の弧
- かなの区間には文字 4-gram の「読みらしさ」（core/kana_lm.py）を足す

上位5件（後段が比べる数）は、1位をコスト最小にしたうえで、残りを Mozc の語の知識も使って選ぶ（_select）。

    uv run python -m core.reading "githubnipushshiteoite"
"""

import argparse
import gc
import heapq
import itertools
import sys
from dataclasses import dataclass
from functools import lru_cache

from .english import FUZZY_MIN_LEN, SHORT_OK, lexicon
from .kana_lm import AFTER_EN, AFTER_NUM, BOS, EOS, ORDER, load as load_lm, word_cost
from .keyboard import NEIGHBORS
from .romaji_table import MAXLEN, TABLE, VOWELS
from .types import Edit, Reading, Seg

# コスト（単位は nats 相当。根拠は docs/READING.md「コスト」）
COST = {
    "sub": 3.0,  # 隣接キーへの置換
    "drop": 3.5,  # 脱字を補う
    "insert": 3.0,  # 余計な1字を捨てる
    "insert_dup": 2.5,  # 余計な1字が隣と同じ字（押しすぎ）
    "swap": 3.0,  # 隣接2字の入れ替え
    "nn": 3.0,  # nn+母音を「ン+な行」と読む（本人は「ん」+母音を nn と打つ。規則外の打ち方の救済）
    "loan": 2.0,  # ti/di を外来音のティ/ディと読む（表では チ/ヂ。user_style に yu-zabiriti- の打ち方がある）
    "en": 2.0,  # 英語区間1つの固定ペナルティ
    "en_tech": 1.5,  # 技術語リストの語の固定ペナルティ（en の代わり）
    "en_freq": 0.3,  # (6.5 - zipf) 1 あたり
    "en_romaji": 1.0,  # 綴りがそのままローマ字として読める一般語（kai, mono…）
    "en_romaji_tech": 0.5,  # 同じく技術語（issue, api…）
    "en_cut": 2.5,  # 子音で終わる英語区間の直後に母音・y（sum|ou、rag|iri のようにローマ字の音節を割っている）
    "compound": 0.5,  # 英字・数字を . - でつないだ塊の、つなぎ1つあたり
    "raw": 5.0,  # 辞書に無い英字列（区間1つの固定分）
    "raw_char": 1.0,  # 辞書に無い英字列の1字あたり
    "raw_token": 2.0,  # かなに読めない位置を含む英数字列（区間1つの固定分）
    "raw_token_brk": 0.15,  # 同じく、かなの弧が1本も出ない英字（jr の j・r）と数字の直前の英字（int8 の int）1字あたり
    "raw_token_ok": 1.0,  # 同じく、かなに読める英字1字あたり（cdma の m・a。raw_char と同じ）
    "num_in_word": 1.5,  # 英字に挟まれた数字（dai2shou は正しいが、打鍵ミスの o→9 なども同じ形になる）
    "num_cut": 2.5,  # 母音・n 以外の英字の直後の数字（ローマ字の音節が途中で切れている）
    "sym_dash": 1.0,  # `-` を長音でなく記号として読む
    "sym_dash_vowel": 3.0,  # 母音の直後の `-` を記号として読む
    "lm_weight": 0.3,  # 文字 4-gram の -log P に掛ける重み
    "lm_cap": 9.0,  # 1字の -log P の上限（ュ・シィのような珍しいかな1字で、正しい入力が編集に負けないように）
    "lm_eos_base": 1.5,  # かなの連続の終わり（EOS）の基準値（WAC の連続の終わりの −log P の平均 1.42）
    "lm_base": 3.5,  # 1字あたりの基準値（これを引いて、英語区間でかなを置き換えても有利不利が出ないようにする）
}

SYMBOLS = {
    ",": ("、", ","), ".": ("。", "."), "?": ("？", "?"), "!": ("！", "!"), "[": ("「", "["), "]": ("」", "]"),
    "/": ("・", "/"), ":": ("：", ":"), "(": ("（", "("), ")": ("）", ")"), "~": ("〜", "~"), ";": ("；", ";"),
}
_CONSONANTS = set("bcdfghjklmpqrstvwxyz")
_LOAN = {"ti": "ティ", "di": "ディ"}
# 編集弧の元にする綴り: 音節表＋「ン」（単独の n は次の字が母音・y でないときだけ。使う側で確かめる）
_EDIT_TABLE = {**TABLE, "n": "ン", "nn": "ン"}
# 編集で直した先にする綴り。x・l で始まる小書き（la → ァ）と q（qa → クァ）は除く: 打とうとした綴りであることが
# まれなのに、母音1字の脱字（a → ァ・クァ）などで弧が増える
_EDIT_TARGETS = {k: v for k, v in _EDIT_TABLE.items() if k[0] not in "xlq"}
_APPEND = "iun"  # 音節の後ろに脱字として補う字（aiueon と比べて WAC test の上位5 が同じで、弧が減って速い）
_EN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-")
RAW_MAX = 10  # かなに読めない位置を含む英数字列の最大長
_EXT = frozenset({"md", "py", "js", "ts", "rs", "sh", "go", "c", "h"})  # . の後ろに来る短い拡張子


@dataclass(frozen=True)
class Arc:
    start: int
    end: int
    kind: str
    text: str
    surfaces: tuple[str, ...] = ()
    parts: tuple[tuple[str, float], ...] = ()  # コストの内訳
    edit: Edit | None = None
    raw: bool = False  # 辞書に無い英字列（隣の raw と出力で1区間にまとめる）

    @property
    def cost(self) -> float:
        return sum(v for _, v in self.parts)


# ---- かなの弧 ----------------------------------------------------------------

@lru_cache(maxsize=1)
def _edit_index() -> dict[str, list[tuple[str, str, int, str, float]]]:
    """打ち間違えた綴り → [(かな, 編集の種類, 綴り内の位置, detail, コスト, 単独の n か)]。音節表の各綴りに1編集を施して作る。"""
    idx: dict[str, list] = {}

    def add(v: str, key: str, kind: str, off: int, detail: str, cost: float):
        if not v or _EDIT_TABLE.get(v) == _EDIT_TABLE[key]:
            return
        idx.setdefault(v, []).append((_EDIT_TABLE[key], kind, off, detail, cost, key == "n"))

    for key in _EDIT_TARGETS:
        n = len(key)
        for k in range(n):
            for nb in NEIGHBORS.get(key[k], ()):
                add(key[:k] + nb + key[k + 1:], key, "sub", k, f"{nb}>{key[k]}", COST["sub"])
            add(key[:k] + key[k + 1:], key, "drop", k, "+" + key[k], COST["drop"])
            if k + 1 < n and key[k] != key[k + 1]:
                sw = key[:k] + key[k + 1] + key[k] + key[k + 2:]
                add(sw, key, "swap", k, f"{sw[k:k + 2]}>{key[k:k + 2]}", COST["swap"])
        for k in range(n + 1):
            near = {key[k - 1]} if k else set()
            if k < n:
                near.add(key[k])
            cands = {c: COST["insert_dup"] for c in near}
            for c in near:
                for nb in NEIGHBORS.get(c, ()):
                    cands.setdefault(nb, COST["insert"])
            for c, cost in cands.items():
                add(key[:k] + c + key[k:], key, "insert", k, "-" + c, cost)
    # 同じ綴り・同じかなの重複は安いほうだけ
    for v, lst in idx.items():
        best: dict[str, tuple] = {}
        for e in lst:
            if e[0] not in best or e[4] < best[e[0]][4]:
                best[e[0]] = e
        idx[v] = list(best.values())
    return idx


def _kana_arcs(s: str, i: int) -> list[Arc]:
    out: list[Arc] = []
    L = len(s)
    ch = s[i]
    nxt = s[i + 1] if i + 1 < L else ""
    for ln in range(1, min(MAXLEN, L - i) + 1):
        k = TABLE.get(s[i:i + ln])
        if k is not None:
            out.append(Arc(i, i + ln, "kana", k))
    if ch == "n":
        if nxt == "n":
            out.append(Arc(i, i + 2, "kana", "ン"))
            nn3 = s[i + 2] if i + 2 < L else ""
            if nn3 and (nn3 in VOWELS or nn3 == "y"):
                for ln in range(2, min(MAXLEN, L - i - 1) + 1):
                    k = TABLE.get(s[i + 1:i + 1 + ln])
                    if k is not None:
                        out.append(Arc(i, i + 1 + ln, "kana", "ン" + k, parts=(("nn", COST["nn"]),)))
        elif nxt == "'":
            out.append(Arc(i, i + 2, "kana", "ン"))
        elif not nxt or (nxt not in VOWELS and nxt != "y"):
            out.append(Arc(i, i + 1, "kana", "ン"))
    if s[i:i + 2] in _LOAN:
        out.append(Arc(i, i + 2, "kana", _LOAN[s[i:i + 2]], parts=(("loan", COST["loan"]),)))
    if ch in _CONSONANTS and (nxt == ch or (ch == "t" and s[i + 1:i + 3] == "ch")):
        out.append(Arc(i, i + 1, "kana", "ッ"))
    if ch == "-":
        out.append(Arc(i, i + 1, "kana", "ー"))
    return out


def _edit_arcs(s: str, i: int) -> list[Arc]:
    out: list[Arc] = []
    L = len(s)
    idx = _edit_index()
    for ln in range(1, min(MAXLEN + 1, L - i) + 1):
        for kana, kind, off, detail, cost, single_n in idx.get(s[i:i + ln], ()):
            if single_n and i + ln < L and (s[i + ln] in VOWELS or s[i + ln] == "y"):
                continue
            out.append(Arc(i, i + ln, "kana", kana, parts=(("edit", cost),), edit=Edit(kind, i + off, detail)))
    # 促音の子音を1つ落とした（kite → キッテ）。直前が同じ子音なら正規の促音が既にある
    c = s[i]
    if c in _CONSONANTS and c not in "nxly" and not (i and s[i - 1] == c):
        for ln in range(2, min(MAXLEN, L - i) + 1):
            k = TABLE.get(s[i:i + ln])
            if k is not None:
                out.append(Arc(i, i + ln, "kana", "ッ" + k, parts=(("edit", COST["drop"]),), edit=Edit("drop", i, "+" + c)))
    # 促音の子音が隣のキーに化けた（tsuku6ta → ツクッタ）・促音の前に余計な1字（iesshin → イッシン）。
    # 促音は前後の字で決まる弧なので、音節表から作る編集の索引に入っていない
    d = s[i + 1] if i + 1 < L else ""
    if d and d in _CONSONANTS and d not in "nxly":
        if c in NEIGHBORS.get(d, ()):
            out.append(Arc(i, i + 1, "kana", "ッ", parts=(("edit", COST["sub"]),), edit=Edit("sub", i, f"{c}>{d}")))
        if i + 2 < L and s[i + 2] == d and (c == d or c in NEIGHBORS.get(d, ())):
            cost = COST["insert_dup"] if c == d else COST["insert"]
            out.append(Arc(i, i + 2, "kana", "ッ", parts=(("edit", cost),), edit=Edit("insert", i, "-" + c)))
    # 音節の後ろの母音1字・n を落とした（tasou → タイソウ、akakohan → アカンコハン）
    for ln in range(1, min(MAXLEN, L - i) + 1):
        k = TABLE.get(s[i:i + ln])
        if k is None:
            continue
        for v in _APPEND:
            out.append(Arc(i, i + ln, "kana", k + _EDIT_TABLE[v], parts=(("edit", COST["drop"]),),
                           edit=Edit("drop", i + ln, "+" + v)))
    # 音節をまたぐ入れ替え（taknaa → タカナア: 1音節目の最後の字と2音節目の最初の字が入れ替わった）
    for j in range(i, min(i + MAXLEN, L - 1)):
        a, b = s[j], s[j + 1]
        if a == b or not (a.isalpha() and b.isalpha()):
            continue
        k1 = _EDIT_TABLE.get(s[i:j] + b)
        if k1 is None or (s[i:j] + b == "n" and a in VOWELS + "y"):
            continue
        for m in range(0, MAXLEN):
            if j + 2 + m > L:
                break
            t2 = a + s[j + 2:j + 2 + m]
            k2 = _EDIT_TABLE.get(t2)
            if t2 == "n" and j + 2 + m < L and s[j + 2 + m] in VOWELS + "y":
                continue
            if k2 is not None:
                out.append(Arc(i, j + 2 + m, "kana", k1 + k2, parts=(("edit", COST["swap"]),),
                               edit=Edit("swap", j, f"{a}{b}>{b}{a}")))
    return out


# ---- 英語・数字・記号の弧 ------------------------------------------------------

def _en_cost(w) -> tuple[tuple[str, float], ...]:
    base = COST["en_tech"] if w.tech else COST["en"]
    freq = COST["en_freq"] * max(0.0, 6.5 - w.zipf)
    if w.tech:
        freq = min(freq, COST["en_freq"])
    parts = [("en", base + freq)]
    if _parses_as_romaji(w.spelling):
        parts.append(("en_romaji", COST["en_romaji_tech" if w.tech else "en_romaji"]))
    return tuple(parts)


def _cut(s: str, j: int, spelling: str) -> tuple[tuple[str, float], ...]:
    last = spelling[-1]
    if j < len(s) and last.isalpha() and last not in VOWELS and (s[j] in VOWELS or s[j] == "y"):
        return (("en_cut", COST["en_cut"]),)
    return ()


@lru_cache(maxsize=100000)
def _parses_as_romaji(w: str) -> bool:
    ok = [False] * (len(w) + 1)
    ok[0] = True
    for i in range(len(w)):
        if ok[i]:
            for a in _kana_arcs(w, i):
                ok[a.end] = True
    return ok[len(w)]


def _en_arcs(s: str, i: int) -> list[Arc]:
    lex = lexicon()
    out: list[Arc] = []
    L = len(s)
    if not s[i].isalpha():
        return out
    for j in range(i + 1, min(L, i + max(lex.maxlen, 17)) + 1):
        if s[j - 1] not in _EN_CHARS:
            break
        w = s[i:j]
        e = lex.get(w)
        if e is not None and (len(w) >= 3 or w in SHORT_OK) and s[j - 1] not in ".-":
            out.append(Arc(i, j, "en", w, e.surfaces, _en_cost(e) + _cut(s, j, w)))
        if w.isalpha() and FUZZY_MIN_LEN - 1 <= len(w) <= 17:
            for f in lex.fuzzy(w):
                if f.word.spelling == w:
                    continue
                cost = COST["insert_dup"] if f.kind == "insert" and f.detail[1:] in (
                    w[f.offset - 1:f.offset], w[f.offset + 1:f.offset + 2]) else COST[f.kind]
                out.append(Arc(i, j, "en", f.word.spelling, f.word.surfaces,
                               _en_cost(f.word) + _cut(s, j, w) + (("edit", cost),),
                               edit=Edit(f.kind, i + f.offset, f.detail)))
    out.extend(_compound_arcs(s, i))
    # 辞書に無い英字列（最後の逃げ道）。3字までと、英字の連続の終わりまで（全長を張ると弧が1位置10本を超えて遅い）
    run_end = i
    while run_end < L and s[run_end].isalpha():
        run_end += 1
    for j in sorted({*range(i + 1, min(run_end, i + 3) + 1), run_end}):
        out.append(Arc(i, j, "en", s[i:j], (s[i:j],), (("raw", COST["raw"] + COST["raw_char"] * (j - i)),), raw=True))
    return out


def _letter_tokens(s: str, p: int, first: bool) -> list[tuple[int, float]]:
    """p から始まる、塊の部品になる英単語 [(終わり, 頻度コスト)]。"""
    lex = lexicon()
    out = []
    for q in range(p + 1, min(len(s), p + lex.maxlen) + 1):
        if not s[q - 1].isalpha():
            break
        w = s[p:q]
        e = lex.get(w)
        nxt = s[q] if q < len(s) else ""
        if len(w) == 1:
            if w != "v" or not nxt.isdigit():  # v3.1 の v だけ1字を許す
                continue
            out.append((q, 0.0))
        elif e is not None and (len(w) >= 3 or w in SHORT_OK or (not first and w in _EXT)
                                or (nxt.isdigit() and e.tech)):
            out.append((q, COST["en_freq"] * max(0.0, 6.5 - e.zipf)))
        elif not first and w in _EXT:
            out.append((q, 0.0))
    return out


def _digit_end(s: str, p: int) -> int:
    q = p
    while q < len(s) and s[q].isdigit():
        q += 1
    return q


def _compound_arcs(s: str, i: int) -> list[Arc]:
    """英字・数字を . - でつないだ塊（claude.md、gpt-2、v3.1、python3.12）。"""
    lex = lexicon()
    out: list[Arc] = []
    L = len(s)
    # (終わり, 部品の数, つなぎの数, 頻度コストの合計, 最後が英字か)
    stack = [(q, 1, 0, fc, True) for q, fc in _letter_tokens(s, i, True)]
    while stack:
        p, ntok, nsep, fsum, last_alpha = stack.pop()
        if ntok >= 2 and nsep + (0 if last_alpha else 1) >= 1:
            w = s[i:p]
            if lex.get(w) is None:
                out.append(Arc(i, p, "en", w, (w,), (("en", COST["en"] + COST["compound"] * (ntok - 1) + fsum / ntok),)
                               + _cut(s, p, w)))
        if ntok >= 4 or p >= L:
            continue
        if last_alpha and s[p].isdigit():
            stack.append((_digit_end(s, p), ntok + 1, nsep, fsum, False))
        if s[p] in ".-" and p + 1 < L:
            if s[p + 1].isdigit():
                stack.append((_digit_end(s, p + 1), ntok + 1, nsep + 1, fsum, False))
            elif s[p + 1].isalpha():
                for q, fc in _letter_tokens(s, p + 1, False):
                    stack.append((q, ntok + 1, nsep + 1, fsum + fc, True))
    return out


def _num_sym_arcs(s: str, i: int) -> list[Arc]:
    ch = s[i]
    L = len(s)
    if ch.isdigit():
        if i and (s[i - 1].isdigit() or (s[i - 1] in ".," and i >= 2 and s[i - 2].isdigit())):
            return []
        j = i
        while j < L and (s[j].isdigit() or (s[j] in ".," and j + 1 < L and s[j + 1].isdigit() and s[j - 1].isdigit())):
            j += 1
        parts = []
        prev = s[i - 1] if i else ""
        if prev.isalpha() and prev not in VOWELS and prev != "n":
            parts.append(("num_cut", COST["num_cut"]))
        elif prev.isalpha() and j < L and s[j].isalpha():
            parts.append(("num_in_word", COST["num_in_word"]))
        return [Arc(i, j, "num", s[i:j], parts=tuple(parts))]
    if ch.isalpha():
        return []
    if ch in ".," and 0 < i < L - 1 and s[i - 1].isdigit() and s[i + 1].isdigit():
        return []  # 数字の中の . , は数字の弧が持つ
    if ch == "'" and i and s[i - 1] == "n":
        return []  # n' の ' はかなの弧が持つ
    if ch == "-":
        # 母音の後の - はふつう長音（かなの弧が持つ）。英字列の後の記号（cve-2024）もありうるので高めに残す
        cost = COST["sym_dash_vowel"] if i and s[i - 1] in VOWELS else COST["sym_dash"]
        return [Arc(i, i + 1, "sym", "-", ("-", "－"), (("sym", cost),))]
    return [Arc(i, i + 1, "sym", ch, SYMBOLS.get(ch, (ch,)))]


_PARTICLES = frozenset({"to", "no", "ni", "wo", "de", "ga", "ha", "mo", "ya", "ka", "he"})


def _raw_arcs(s: str, i: int, brk: list[bool], cheap: list[bool]) -> list[Arc]:
    """かなに読めない字を含む英数字列（jr、cdma、bf16、rtx5060ti）。辞書に無い略語・型番を英字のまま残す経路。
    brk は「かなの弧が1本も出ない英字」、cheap は brk か「すぐ後ろに数字が続く英字の連続」（int8 の int）。"""
    out: list[Arc] = []
    if not s[i].isalpha():
        return out
    L = len(s)
    cost = COST["raw_token"]
    has = False
    stretch = None  # いま続いている、かなに読める英字の連続の始まり
    for j in range(i + 1, min(L, i + RAW_MAX) + 1):
        c = s[j - 1]
        if c.isalpha() and not brk[j - 1]:
            if stretch is None:
                stretch = j - 1
        elif c.isalpha() or c.isdigit():
            # 読めない字・数字にはさまれた助詞の綴り（sram|to|dram、bf16|to|fp16）は区切りとみて、そこで止める
            # 区間の頭が助詞の綴りで始まる形（a100|toh100 の toh100）も作らない
            if stretch is not None and s[stretch:j - 1] in _PARTICLES:
                break
            stretch = None
        else:
            break
        if c.isalpha():
            cost += COST["raw_token_brk"] if cheap[j - 1] else COST["raw_token_ok"]
            has = has or cheap[j - 1]
        if not has or j - i < 2 or (c.isdigit() and j < L and s[j].isdigit()):
            continue
        out.append(Arc(i, j, "en", s[i:j], (s[i:j],), (("raw", cost),), raw=True))
    return out


def build_arcs(keys: str) -> list[list[Arc]]:
    """各位置から出る弧。"""
    s = keys.lower()
    L = len(s)
    kana = [_kana_arcs(s, i) for i in range(L)]
    brk = [s[i].isalpha() and not kana[i] for i in range(L)]
    pre_digit = [False] * (L + 1)  # すぐ後ろに数字が続く英字の連続
    for i in range(L - 2, -1, -1):
        pre_digit[i] = s[i].isalpha() and (s[i + 1].isdigit() or pre_digit[i + 1])
    cheap = [b or d for b, d in zip(brk, pre_digit)]
    arcs = []
    for i in range(L):
        a = kana[i] + _edit_arcs(s, i) + _en_arcs(s, i) + _num_sym_arcs(s, i) + _raw_arcs(s, i, brk, cheap)
        arcs.append(a)
    return arcs


# ---- 探索 --------------------------------------------------------------------

_START = BOS * (ORDER - 1)
# 英字・数字の区間の後のかなは、文頭でなく「英字・数字の語の直後」の文脈で数える（jr|ノ、3.12|ニ）
_CTX_AFTER = {"en": BOS * (ORDER - 2) + AFTER_EN, "num": BOS * (ORDER - 2) + AFTER_NUM}


@lru_cache(maxsize=None)  # 文脈2字×弧のかな（4字以下）なので、実際に現れる組は数十万で頭打ちになる
def _lm_step(ctx: str, text: str) -> tuple[float, str]:
    lm = load_lm()
    total = 0.0
    h = ctx
    for c in text:
        total += min(-lm.logp(h, c), COST["lm_cap"]) - COST["lm_base"]
        h = (h + c)[-(ORDER - 1):]
    return COST["lm_weight"] * total, h


@lru_cache(maxsize=None)
def _lm_end(ctx: str) -> float:
    """かなの連続が英字・数字の区間の前で終わる読みらしさ（オ|cr、ギ|thib のような切れ目は高い）。"""
    if ctx[-1] in (BOS, AFTER_EN, AFTER_NUM):
        return 0.0
    return COST["lm_weight"] * (min(-load_lm().logp(ctx, EOS), COST["lm_cap"]) - COST["lm_eos_base"])


def _segs(path: list[Arc]) -> tuple[Seg, ...]:
    segs: list[Seg] = []
    prev_raw = False
    for a in path:
        if segs and a.kind == "kana" and segs[-1].kind == "kana":
            p = segs[-1]
            segs[-1] = Seg("kana", p.start, a.end, p.text + a.text)
        elif segs and a.raw and prev_raw:
            p = segs[-1]
            t = p.text + a.text
            segs[-1] = Seg("en", p.start, a.end, t, (t,))
        else:
            segs.append(Seg(a.kind, a.start, a.end, a.text, a.surfaces))
        prev_raw = a.raw
    return tuple(segs)


BEAM = 10.0  # 同じ位置で最良からこれ以上悪い状態は捨てる（実際は MAX_STATES のほうが先に効く）
POS_PRUNE = 10.0  # 到達先の位置の最良よりこれ以上悪い遷移は記録しない（状態の上限で必ず落ちる）
MIN_PER_EDITS = 4  # 編集数ごとに必ず残す状態の数
MAX_STATES = 16  # 同じ位置で残す状態の数の上限（24/6 は AJIMEE 上位5 +1〜2 件で所要が約3割増。docs/READING.md）
# 探索で数える編集のコスト。COST の値以下にする（返す Reading.cost は COST のまま。_select を参照）。
# WAC test で調整（打鍵ミスの起こりやすさ data/noise.py に近づけると、入れ替え・押しすぎが安い）
SEARCH_EDIT = {"sub": 2.0, "drop": 2.0, "insert": 2.0, "insert_dup": 1.5, "swap": 1.5}
TOP_BLOCK = 5  # 後段が比べる上位の件数（core/pipeline.py の k）
POOL_EXTRA = 20  # 上位 TOP_BLOCK 件を選ぶために、n 件より余分に取り出す数
RAW_QUOTA = 2  # 上位 TOP_BLOCK 件のうち、辞書に無い英字列を含む候補の上限
# 上位 TOP_BLOCK 件の2位以降を選ぶ点数: 探索のコスト + WORD_WEIGHT × 語のコストの差（1位との）
# + RR_LM × 読みらしさ + RR_EDIT × 編集数。語のコストは Mozc の読みと生起コストで、かなの連続を語に分けた最小
# （core/kana_lm.py の WordCost。語1つに WORD_CONN を足す）。値は WAC test で調整（docs/READING.md）
WORD_WEIGHT = 0.0001
WORD_CONN = 6000.0
WORD_UNK = 15000.0
RR_LM = -0.5
RR_EDIT = -0.5


def readings(keys: str, n: int = 20, max_edits: int = 2) -> list[Reading]:
    """打鍵列 keys の読みの候補を最大 n 個返す（同じ区間列の重複は除く）。1位はコスト最小。上位 TOP_BLOCK 件は
    打鍵ミスの直し方が違う候補を広く拾うように選ぶ（_select）ので、上位 TOP_BLOCK 件とその後ろは、それぞれの中で
    コストの小さい順。"""
    # 探索は1回で数十万個のタプル・リストを作り、その間に世代別 GC が何度も走って全部を走査する
    # （AJIMEE 20〜39字の p95 が GC ありで約280ms、なしで約40ms。クラウドの Xeon で実測）。
    # 作るものは循環参照を持たず、戻るときに参照カウントで解放されるので、探索の間だけ GC を止める
    enabled = gc.isenabled()
    gc.disable()
    try:
        return _readings(keys, n, max_edits)
    finally:
        if enabled:
            gc.enable()


def _search_cost(a: Arc) -> float:
    if a.edit is None:
        return a.cost
    e = sum(v for k, v in a.parts if k == "edit")
    kind = "insert_dup" if a.edit.kind == "insert" and e == COST["insert_dup"] else a.edit.kind
    return a.cost - e + SEARCH_EDIT[kind]


def _readings(keys: str, n: int, max_edits: int) -> list[Reading]:
    L = len(keys)
    if L == 0:
        return [Reading(())]
    arcs = []
    for lst in build_arcs(keys):
        # 終点・種類・文字列・編集の有無が同じ弧は、探索では安いほうだけ使う
        best: dict[tuple, Arc] = {}
        for a in lst:
            k = (a.end, a.kind, a.text, a.edit is not None, a.raw)
            if k not in best or a.cost < best[k].cost:
                best[k] = a
        # 探索では編集を SEARCH_EDIT のコストで数える（返すコストは元のまま。_select を参照）
        kana, other = [], []
        for a in best.values():
            if a.kind == "kana":
                kana.append((a.end, _search_cost(a), a.edit is not None, a.text, a))
            else:
                other.append((a.end, _search_cost(a), a.edit is not None, _CTX_AFTER.get(a.kind, _START), a.raw, a))
        arcs.append((kana, other))
    # 前向き: 状態 (位置, 編集数, かなの文脈) ごとの最良コストと、入ってくる遷移を記録する
    g: list[dict] = [dict() for _ in range(L + 1)]
    pbest = [float("inf")] * (L + 1)  # 位置ごとの最良
    preds: dict[tuple, list] = {}
    g[0][(0, _START)] = 0.0
    lm_step, lm_end = _lm_step, _lm_end
    for i in range(L):
        here = g[i]
        if not here:
            continue
        cutoff = min(here.values()) + BEAM
        # 状態数の上限: 全体の上位 MAX_STATES に加え、編集数ごとの上位 MIN_PER_EDITS も残す
        # （全体だけだと、編集を2つ要する正解が編集0の仮説に押し出される）
        keep: dict[int, float] = {}
        if len(here) > MAX_STATES:
            vals = sorted(here.values())
            cutoff = min(cutoff, vals[MAX_STATES - 1])
            by_e: dict[int, list[float]] = {}
            for (e, _), gi in here.items():
                by_e.setdefault(e, []).append(gi)
            keep = {e: sorted(lst)[min(MIN_PER_EDITS, len(lst)) - 1] for e, lst in by_e.items()}
        kana, other = arcs[i]
        for (e, ctx), gi in here.items():
            if gi > cutoff and gi > keep.get(e, cutoff):
                continue
            src = (i, e, ctx)
            end_lm = None
            for end, cost, is_edit, text, a in kana:
                ne = e + is_edit
                if ne > max_edits:
                    continue
                lmc, nctx = lm_step(ctx, text)
                c = cost + lmc
                ng = gi + c
                # 位置の最良より POS_PRUNE 以上悪い遷移は、状態の上限で必ず落ちるので記録もしない
                if ng > pbest[end] + POS_PRUNE:
                    continue
                if ng < pbest[end]:
                    pbest[end] = ng
                key = (ne, nctx)
                tgt = g[end]
                old = tgt.get(key)
                if old is None or ng < old:
                    tgt[key] = ng
                preds.setdefault((end, ne, nctx), []).append((src, c, lmc, a))
            for end, cost, is_edit, nctx, raw, a in other:
                ne = e + is_edit
                if ne > max_edits:
                    continue
                if raw:  # 辞書に無い英字列の前で終わるかなの連続（オ|cr のような切れ目）の読みらしさ
                    if end_lm is None:
                        end_lm = lm_end(ctx)
                    lmc = end_lm
                else:
                    lmc = 0.0
                c = cost + lmc
                ng = gi + c
                if ng > pbest[end] + POS_PRUNE:
                    continue
                if ng < pbest[end]:
                    pbest[end] = ng
                key = (ne, nctx)
                tgt = g[end]
                old = tgt.get(key)
                if old is None or ng < old:
                    tgt[key] = ng
                preds.setdefault((end, ne, nctx), []).append((src, c, lmc, a))
    if not g[L]:
        return []
    return _select(_extract(g, preds, L), n)


def _select(paths, n: int) -> list[Reading]:
    """取り出した候補（探索のコスト順）から n 件を選ぶ。

    1位は元のコスト（Reading.cost）の最小なので、きれいな入力の1位と誤補正は探索で編集を安くしても変わらない。
    2〜TOP_BLOCK 位は、取り出した n + POOL_EXTRA 件から、語のコストを足した点数（上の WORD_WEIGHT の説明）の順。
    辞書に無い英字列を含む候補は RAW_QUOTA 件までにし（打鍵ミスでできた子音の並びを英字のまま残す変種が
    枠を埋めないように）、1件も入らなければ最後の枠をそのうちコストが最小のものにする（「英字として読む」仮説）。
    上位 TOP_BLOCK 件とその後ろは、それぞれ元のコスト順に並べる。
    """
    got: list[list] = []  # [Reading, 辞書に無い英字列を含むか, 探索のコスト]（同じ区間列は1つ）
    index: dict[tuple, int] = {}
    best = float("inf")

    def take(r: Reading, has_raw: bool, f: float, sig: tuple) -> None:
        k = index.get(sig)
        if k is None:
            index[sig] = len(got)
            got.append([r, has_raw, f])
        elif r.cost < got[k][0].cost:
            # 同じ区間列が、編集を使う経路と使わない経路の両方で作れる（ジッケンノケッカ: 脱字 n と nn の揺れ）。
            # 探索は編集を安く数えるので高い経路が先に出ることがある。返すコストは安いほう
            got[k][0], got[k][1] = r, has_raw

    pending = None
    for r, has_raw, f, sig in paths:
        # 元のコスト >= 探索のコスト（編集を安く数えるだけ）なので、取り出し中の探索のコストが
        # 元のコストの最小を超えたら、まだ出ていない候補が元のコストで勝つことはない
        if len(got) >= n + POOL_EXTRA and f >= best:
            pending = (r, has_raw, f, sig)
            break
        take(r, has_raw, f, sig)
        best = min(best, r.cost)
    if not got:
        return []
    first = min(range(len(got)), key=lambda k: got[k][0].cost)
    order = sorted(range(len(got)), key=lambda k: got[k][2])
    wc = word_cost(WORD_CONN, WORD_UNK)
    if wc is not None:
        w = [sum(wc(sg.text) for sg in r.segs if sg.kind == "kana") for r, _, _ in got]
        order.sort(key=lambda k: got[k][2] + WORD_WEIGHT * (w[k] - w[first])
                   + RR_LM * got[k][0].breakdown.get("lm", 0.0) + RR_EDIT * len(got[k][0].edits))
    size = min(n, TOP_BLOCK)
    used = [first]
    n_raw = int(got[first][1])
    for k in order:
        if len(used) >= size:
            break
        if k == first or (got[k][1] and n_raw >= RAW_QUOTA):
            continue
        n_raw += got[k][1]
        used.append(k)
    # 「直さない」と「英字として読む」の仮説を1つずつ残す（後ろの枠から入れ替える）
    slot = len(used) - 1
    for want in (lambda k: not got[k][0].edits, lambda k: got[k][1]):
        if slot < 1 or len(used) < size or any(want(k) for k in used):
            continue
        cand = [k for k in range(len(got)) if want(k) and k not in used]
        if cand:
            used[slot] = min(cand, key=lambda k: got[k][0].cost)
            slot -= 1
    # 選んだ候補のコストが、まだ取り出していない同じ区間列の経路より高くないことを確かめる
    limit = max(got[k][0].cost for k in used)
    for r, has_raw, f, sig in itertools.chain([pending] if pending else [], paths):
        if f >= limit:
            break
        if sig in index:
            take(r, has_raw, f, sig)
    top = sorted((got[k][0] for k in used), key=lambda r: r.cost)
    chosen = set(used)
    rest = sorted((r for k, (r, _, _) in enumerate(got) if k not in chosen), key=lambda r: r.cost)
    return top + rest[:n - len(top)]


def _extract(g: list[dict], preds: dict, L: int):
    """後ろ向きに最良優先で経路を取り出す（前向きの最良コストが正確な残りコストになるので、取り出し順は探索のコスト順）。
    (Reading, 辞書に無い英字列を含むか, 探索のコスト, 区間列) を順に返す（同じ区間列が何度も出る）。"""
    tie = itertools.count()
    heap = [(gv, next(tie), 0.0, (L, e, ctx), None) for (e, ctx), gv in g[L].items()]
    heapq.heapify(heap)
    pops = 0
    while heap and pops < 20000:
        f, _, back, state, node = heapq.heappop(heap)
        pops += 1
        if state == (0, 0, _START):
            path: list[tuple[Arc, float]] = []
            while node is not None:
                a, lmc, node = node
                path.append((a, lmc))
            arcs_only = [a for a, _ in path]
            segs = _segs(arcs_only)
            sig = tuple((sg.kind, sg.text, sg.end) for sg in segs)
            edits = tuple(a.edit for a in arcs_only if a.edit is not None)
            bd: dict[str, float] = {}
            for a, lmc in path:
                for k, v in a.parts:
                    bd[k] = bd.get(k, 0.0) + v
                if lmc:
                    bd["lm"] = bd.get("lm", 0.0) + lmc
            cost = round(sum(bd.values()), 3)
            yield Reading(segs, edits, cost, {k: round(v, 3) for k, v in bd.items()}), any(a.raw for a in arcs_only), f, sig
            continue
        for src, c, lmc, a in preds.get(state, ()):
            gs = g[src[0]].get(src[1:])
            if gs is None:
                continue
            nb = back + c
            heapq.heappush(heap, (gs + nb, next(tie), nb, src, (a, lmc, node)))


# ---- CLI ---------------------------------------------------------------------

def _fmt(r: Reading) -> str:
    segs = " ".join(
        f"{s.text}" if s.kind == "kana" else f"[{s.kind}:{s.text}" + (f"→{s.surfaces[0]}" if s.surfaces and s.surfaces[0] != s.text else "") + "]"
        for s in r.segs)
    edits = ", ".join(f"{e.kind}@{e.pos}:{e.detail}" for e in r.edits)
    bd = " ".join(f"{k}={v:+.2f}" for k, v in r.breakdown.items())
    return f"{r.cost:7.2f}  {r.zenz_input()}\n         区間: {segs}" + (f"\n         編集: {edits}" if edits else "") + f"\n         内訳: {bd}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="打鍵列の読みの N-best とコストの内訳を表示する")
    ap.add_argument("keys", nargs="+")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--max-edits", type=int, default=2)
    args = ap.parse_args(argv)
    for keys in args.keys:
        print(f"# {keys}")
        for k, r in enumerate(readings(keys, args.n, args.max_edits), 1):
            print(f"{k:2d} {_fmt(r)}")


if __name__ == "__main__":
    main(sys.argv[1:])
