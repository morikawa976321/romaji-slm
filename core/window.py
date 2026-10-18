"""常駐入力（ime/）の候補ウィンドウ: 何をどの順で並べるか（docs/decisions.md D37）。

候補の出どころは 2 つ。

- 読みの候補（CorePipeline.candidates）: 読みの上位 k 件をそれぞれ変換したもの。打鍵を直した読み
  （iku → 以降・行き・奥・肉）を含む。
- 1 位の読みの別の書き方: 1 位の読みの辞書ラティスの N-best（core.kanakanji.convert。ユーザー辞書の語も入る）を、
  1 位を決めるのと同じ式（zenz の変換の対数確率 + f × 完成文の自然さ − w × 読みのコスト）で採点したもの
  （逝く・往く・イク・幾）。zenz の採点は 1 回のバッチ（変換の確率と自然さを合わせて 2 × 件数の系列）。

1 位はどの並べ方でも CorePipeline.convert と同じ文。最後の枠は 1 位の読みのひらがな（辞書に無い語を打ったときの逃げ道）。

並べ方（ORDERS）:

- reading: 読みの候補だけを点数順に（2026-10-18 までの並べ方）。
- same_first: 1 位の読みの別の書き方を先に、読みの候補をその後ろに、それぞれ点数順。
- merge: 両方を点数順に混ぜる。
- quota: merge のうえで、直した読みを edit_slots 件まで窓に残す（入りきらないときは点数の低い別の書き方と入れ替える）。
"""

import time
import unicodedata
from dataclasses import dataclass, field

from eval.prompts import CONTEXT, INPUT

from .constrained import LatticeMatcher, bounds_from_text
from .kanakanji import convert
from .pipeline import render_en_case
from .select import scoring_text, zenz_prompt

ORDERS = ("reading", "same_first", "merge", "quota")
DEFAULT_ORDER = "quota"
EDIT_SLOTS = 1  # quota で窓に残す直した読みの件数
N_ALT = 10      # 1 位の読みの N-best を何件まで採点するか
_PUNCT = frozenset("、。？！?!,.")
# 別の書き方どうしを、記号の全角・半角の違いだけなら同じとみなす（ラティスは両方の幅を持つ。窓を幅違いで埋めない）
_SYM_KEY = str.maketrans("、。？！「」・：，．", ",.?![]/:,.")


@dataclass
class Entry:
    text: str       # 表示する文（英字の表記・文末の記号を描画済み）
    score: float    # 1 位と同じ式の点数
    kind: str       # "same"（1 位の読みの別の書き方）/ "typed"（直していない別の読み）/ "edit"（直した読み）
    reading: str    # 読み（zenz に渡すカタカナ）
    edited: bool    # 読みに打鍵の直しが入っているか（ユーザー辞書の fix を含む）


@dataclass
class Pool:
    first: Entry                 # 1 位（convert と同じ文）
    hira: Entry | None           # 最後の枠: 1 位の読みのひらがな
    entries: list[Entry]         # 1 位以外。読みの候補は点数順、別の書き方は点数順
    ms: dict = field(default_factory=dict)   # 段ごとの所要（candidates / alt）


# 幅の対（全角, 半角）。別の書き方の幅を 1 位に揃える
_WIDTH_PAIRS = tuple(zip("０１２３４５６７８９、。！？", "0123456789,.!?"))


def _key(t: str) -> str:
    return unicodedata.normalize("NFKC", t.translate(_SYM_KEY))


def _align_width(t: str, first: str) -> str:
    """数字と句読点の幅を 1 位に揃える。ラティスは数の読みに 300・３００、記号に ！・! の両方を持ち、辞書の N-best は
    1 位（zenz が選んだ幅）と違う幅を選びうる。1 位が両方の幅を使っている字は触らない。"""
    digits_w = any("０" <= ch <= "９" for ch in first)
    digits_n = any("0" <= ch <= "9" for ch in first)
    table = {}
    for w, n in _WIDTH_PAIRS:
        has_w, has_n = (digits_w, digits_n) if w.isdigit() else (w in first, n in first)
        if has_w and not has_n:
            table[ord(n)] = w
        elif has_n and not has_w:
            table[ord(w)] = n
    return t.translate(table)


def _with_end(t: str, end: str) -> str:
    return t + end if end and t and t[-1] not in _PUNCT else t


def alternatives(p, best: dict, left_context: str = "", end: str = "", n_alt: int = N_ALT) -> list[Entry]:
    """1 位の読みの別の書き方を、1 位と同じ式で採点して点数順に返す（1 位の文そのものは除く）。

    制約付き生成と同じ照合器（core.constrained.LatticeMatcher）を通るものだけを残す。句読点・英字の表記モードと、
    辞書に無いかなの断片の扱い（小書き・ンで始まる断片を許さない）が 1 位と揃う。"""
    r = best["reading_obj"]
    m = LatticeMatcher(r, p.dic, p.punct, p.en_case)
    seen = {_key(best["text"])}
    texts = []
    for conv in convert(r, p.dic, n=n_alt):
        # ラティスの英語区間は打った綴りだけを生成させている（core.constrained._en_surface）。辞書の N-best は登録表記も持つ
        t = "".join(r.segs[w.seg_index].text if w.source == "en" and p.en_case != "zenz" else w.surface
                    for w in conv.words)
        k = _key(t)
        if not t or k in seen:
            continue
        ok = lambda s: m.accepting(m.advance(m.start(), scoring_text(s).encode()))  # noqa: E731
        aligned = _align_width(t, best["text"])
        if aligned != t and ok(aligned):
            t = aligned
        elif not ok(t):
            continue
        seen.add(k)
        texts.append(t)
    if not texts:
        return []
    n = len(texts)
    prompt = zenz_prompt(r.zenz_input(), left_context)
    head = CONTEXT + left_context   # core.select.fluency と同じ置き方
    lp = p.scorer.logprobs([prompt] * n + [head] * n,
                           [scoring_text(t) for t in texts] + [scoring_text(t) + INPUT for t in texts],
                           eos=[True] * n + [False] * n)
    edited = bool(best["edits"])
    out = []
    for i, t in enumerate(texts):
        score = lp[i] + p.w_flu * lp[n + i] - p.w_read * r.cost
        text = render_en_case(_with_end(t, end), r, p.en_case, bounds_from_text(m, t))
        out.append(Entry(text, score, "same", best["reading"], edited))
    return sorted(out, key=lambda e: -e.score)


def pool(p, romaji: str, left_context: str = "", alts: bool = True, n_alt: int = N_ALT) -> Pool | None:
    """窓に並べうる候補を集める。alts=False なら読みの候補だけ（2026-10-18 までと同じ計算）。

    llama-server 版（p.backend あり）は別の書き方を採点する手段を持たないので、読みの候補だけにする。"""
    t0 = time.perf_counter()
    cands = p.candidates(romaji, left_context)
    if not cands:
        return None
    best = cands[0]
    first_text = p._render_best(best, romaji, left_context)
    t1 = time.perf_counter()
    # 2 位以下は句読点を補う採点をし直さず（1 件ごとに 100 ms 以上かかる）、1 位が文末に補った記号だけを同じく付ける
    end = first_text[-1] if first_text and first_text[-1] in "。？" and not best["text"].endswith(first_text[-1]) else ""
    first = Entry(first_text, best["score"], "first", best["reading"], bool(best["edits"]))
    entries = [Entry(render_en_case(_with_end(c["text"], end), c["reading_obj"], p.en_case, c["bounds"]), c["score"],
                     "edit" if c["edits"] else "typed", c["reading"], bool(c["edits"]))
               for c in cands[1:] if c["score"] != float("-inf")]
    hira_text = _with_end("".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in best["reading"]), end)
    hira = Entry(hira_text, float("-inf"), "hira", best["reading"], bool(best["edits"])) if hira_text else None
    if alts and p.backend is None and best["score"] != float("-inf"):
        entries += alternatives(p, best, left_context, end, n_alt)
    t2 = time.perf_counter()
    return Pool(first, hira, entries, {"candidates": (t1 - t0) * 1000, "alt": (t2 - t1) * 1000})


def arrange(pl: Pool, order: str = DEFAULT_ORDER, n: int = 6, edit_slots: int = 1) -> list[Entry]:
    """Pool から窓の中身（1 位を先頭、1 位の読みのひらがなを最後に、重複なしで最大 n 件）を作る。"""
    if order not in ORDERS:
        raise ValueError(f"order は {ORDERS} のどれか: {order}")
    same = [e for e in pl.entries if e.kind == "same"]
    reads = [e for e in pl.entries if e.kind != "same"]
    by_score = lambda es: sorted(es, key=lambda e: -e.score)  # noqa: E731（sorted は安定。同点は元の順）
    if order == "reading":
        body = reads
    elif order == "same_first":
        body = by_score(same) + by_score(reads)
    else:
        body = by_score(same + reads)
    hira = pl.hira.text if pl.hira else ""
    # reading 以外は、記号の幅・NFKC だけが違う候補も同じとみなす（了解！ と 了解! で枠を使わない）
    key = (lambda t: t) if order == "reading" else _key  # noqa: E731
    seen = {key(pl.first.text), key(hira)}
    # reading は置き換える前と同じ列にするため、ひらがなが 1 位と同じでも枠を 1 つ空けたままにする
    room = n - 2 if order == "reading" or (hira and hira != pl.first.text) else n - 1
    picked: list[Entry] = []
    rest: list[Entry] = []
    for e in body:
        if not e.text or key(e.text) in seen:
            continue
        seen.add(key(e.text))
        (picked if len(picked) < room else rest).append(e)
    if order == "quota":
        edits_in = sum(e.kind == "edit" for e in picked)
        for e in [e for e in rest if e.kind == "edit"][: max(0, edit_slots - edits_in)]:
            drop = [i for i, x in enumerate(picked) if x.kind == "same"]
            if not drop:
                break
            picked.pop(drop[-1])   # 点数の最も低い別の書き方と入れ替える
            picked.append(e)
        picked = by_score(picked)
    out = [pl.first] + picked
    return out + [pl.hira] if pl.hira and pl.hira.text != pl.first.text else out


def nbest(p, romaji: str, left_context: str = "", n: int = 6, order: str = DEFAULT_ORDER,
          edit_slots: int | None = None) -> list[str]:
    """常駐の入力が次候補に使う、重複のない候補の列。1 位は convert と同じ文。edit_slots は quota の枠（None で EDIT_SLOTS）。"""
    pl = pool(p, romaji, left_context, alts=order != "reading")
    if pl is None:
        return []
    return [e.text for e in arrange(pl, order, n, EDIT_SLOTS if edit_slots is None else edit_slots)]
