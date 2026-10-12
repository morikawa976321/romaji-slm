"""打鍵列 → 読みの N-best（docs/REDESIGN.md 工程2「読みラティス」。仕組みとコストの根拠は docs/READING.md）。

打鍵列の文字位置を節点とし、次の弧を張って、コストの小さい順に経路を N 本取り出す。

- 正規ローマ字の弧（コスト0）。`nn`+母音を「ン+な行」と読む弧は小さいコスト
- 打鍵ミスの編集弧（隣接キーへの置換・脱字・余計な1字・隣接2字の入れ替え）。1弧1編集、1文 max_edits まで
- 英語の弧（wordfreq の上位語＋core/data/tech_words.tsv）、数字の弧、記号の弧
- かなの区間には文字 4-gram の「読みらしさ」（core/kana_lm.py）を足す

    uv run python -m core.reading "githubnipushshiteoite"
"""

import argparse
import heapq
import itertools
import sys
from dataclasses import dataclass
from functools import lru_cache

from .english import FUZZY_MIN_LEN, SHORT_OK, lexicon
from .kana_lm import BOS, ORDER, load as load_lm
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
    "num_in_word": 1.5,  # 英字に挟まれた数字（dai2shou は正しいが、打鍵ミスの o→9 なども同じ形になる）
    "num_cut": 2.5,  # 母音・n 以外の英字の直後の数字（ローマ字の音節が途中で切れている）
    "sym_dash": 1.0,  # `-` を長音でなく記号として読む
    "lm_weight": 0.3,  # 文字 4-gram の -log P に掛ける重み
    "lm_cap": 9.0,  # 1字の -log P の上限（ュ・シィのような珍しいかな1字で、正しい入力が編集に負けないように）
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
_APPEND = "iun"  # 音節の後ろに脱字として補う字（aiueon と比べて WAC test の上位5 が同じで、弧が減って速い）
_EN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-")
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

    for key in _EDIT_TABLE:
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
        if i and s[i - 1] in VOWELS:
            return []
        return [Arc(i, i + 1, "sym", "-", ("-", "－"), (("sym", COST["sym_dash"]),))]
    return [Arc(i, i + 1, "sym", ch, SYMBOLS.get(ch, (ch,)))]


def build_arcs(keys: str) -> list[list[Arc]]:
    s = keys.lower()
    arcs = []
    for i in range(len(s)):
        a = _kana_arcs(s, i) + _edit_arcs(s, i) + _en_arcs(s, i) + _num_sym_arcs(s, i)
        arcs.append(a)
    return arcs


# ---- 探索 --------------------------------------------------------------------

_START = BOS * (ORDER - 1)


@lru_cache(maxsize=None)  # 文脈2字×弧のかな（4字以下）なので、実際に現れる組は数十万で頭打ちになる
def _lm_step(ctx: str, text: str) -> tuple[float, str]:
    lm = load_lm()
    total = 0.0
    h = ctx
    for c in text:
        total += min(-lm.logp(h, c), COST["lm_cap"]) - COST["lm_base"]
        h = (h + c)[-(ORDER - 1):]
    return COST["lm_weight"] * total, h


def _step(ctx: str, a: Arc) -> tuple[float, str]:
    if a.kind == "kana":
        return _lm_step(ctx, a.text)
    return 0.0, _START


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
MIN_PER_EDITS = 3  # 編集数ごとに必ず残す状態の数
MAX_STATES = 12  # 同じ位置で残す状態の数の上限（WAC test で 16→12 は上位5 が同じ、所要は約3割減）


def readings(keys: str, n: int = 20, max_edits: int = 2) -> list[Reading]:
    """打鍵列 keys の読みの候補を、コストの小さい順に最大 n 個返す（同じ区間列の重複は除く）。"""
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
        arcs.append([(a.end, a.cost, a.edit is not None, a.kind == "kana", a) for a in best.values()])
    # 前向き: 状態 (位置, 編集数, かなの文脈) ごとの最良コストと、入ってくる遷移を記録する
    g: list[dict] = [dict() for _ in range(L + 1)]
    preds: dict[tuple, list] = {}
    g[0][(0, _START)] = 0.0
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
        for (e, ctx), gi in here.items():
            if gi > cutoff and gi > keep.get(e, cutoff):
                continue
            src = (i, e, ctx)
            for end, cost, is_edit, is_kana, a in arcs[i]:
                ne = e + is_edit
                if ne > max_edits:
                    continue
                if is_kana:
                    lmc, nctx = _lm_step(ctx, a.text)
                    c = cost + lmc
                else:
                    lmc, nctx, c = 0.0, _START, cost
                key = (ne, nctx)
                ng = gi + c
                tgt = g[end]
                old = tgt.get(key)
                if old is None or ng < old:
                    tgt[key] = ng
                preds.setdefault((end, ne, nctx), []).append((src, c, lmc, a))
    if not g[L]:
        return []
    # 後ろ向きに最良優先で経路を取り出す（前向きの最良コストが正確な残りコストになるので、取り出し順はコスト順）
    tie = itertools.count()
    heap = [(gv, next(tie), 0.0, (L, e, ctx), None) for (e, ctx), gv in g[L].items()]
    heapq.heapify(heap)
    out: list[Reading] = []
    seen: set = set()
    pops = 0
    while heap and len(out) < n and pops < 20000:
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
            if sig in seen:
                continue
            seen.add(sig)
            bd: dict[str, float] = {}
            for a, lmc in path:
                for k, v in a.parts:
                    bd[k] = bd.get(k, 0.0) + v
                if a.kind == "kana":
                    bd["lm"] = bd.get("lm", 0.0) + lmc
            bd = {k: round(v, 3) for k, v in bd.items()}
            edits = tuple(a.edit for a in arcs_only if a.edit is not None)
            out.append(Reading(segs, edits, round(back, 3), bd))
            continue
        for src, c, lmc, a in preds.get(state, ()):
            gs = g[src[0]].get(src[1:])
            if gs is None:
                continue
            nb = back + c
            heapq.heappush(heap, (gs + nb, next(tie), nb, src, (a, lmc, node)))
    return out


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
