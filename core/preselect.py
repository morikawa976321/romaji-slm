"""読み段の候補を、辞書の変換コストで並べ直して後段に渡す件数に絞る（docs/READING.md「未解決の論点」1 の案）。

読み段（core.reading.readings）の候補 20〜40 件のうち、1位（読み段のコスト最小）はそのまま残し、2位以降を
「読み段の探索のコスト ＋ CONV_WEIGHT × 辞書の変換コストの差（1位との）＋ RR_LM × 読みらしさ ＋ RR_EDIT × 編集数」
の小さい順に選ぶ。変換コストは core.kanakanji の辞書ラティスの最良経路のコスト（Mozc の生起コスト＋接続コスト）。
辞書の語で説明できない読み（打鍵ミスでできたかなの並び）は変換コストが高いので下がる。

    from core.kanakanji import Dictionary
    from core.preselect import preselect
    rs = preselect(readings(keys, n=30), Dictionary.load(), k=5)
"""

from .english import lexicon
from .kanakanji import Dictionary, _build_lattice
from .reading import SEARCH_EDIT
from .types import Reading

_INF = float("inf")

# 値は WAC test（調整用）の候補を書き出して机上で選んだ（docs/READING.md「難しい打鍵ミス」の「残した案」）
CONV_WEIGHT = 0.0003
RR_LM = -0.5
RR_EDIT = -0.5
RAW_QUOTA = 2  # 選ぶ候補のうち、辞書に無い英字列（綴りだけの英語区間）を含むものの上限（core.reading と同じ）


def conv_cost(reading: Reading, dic: Dictionary) -> float:
    """辞書ラティスの最良経路のコスト（core.kanakanji.convert の1位のコストと同じ値。前向きだけ計算する）。"""
    lat, last = _build_lattice(reading, dic)
    num = len(lat.start)
    starts_at: list[list[int]] = [[] for _ in range(last + 2)]
    ends_at: list[list[int]] = [[] for _ in range(last + 2)]
    for v in range(1, num):
        starts_at[lat.start[v]].append(v)
    for v in range(num - 1):
        ends_at[lat.end[v]].append(v)
    matrix, size = dic.matrix, dic.pos_size
    lid, rid, wcost = lat.lid, lat.rid, lat.cost
    alpha = [_INF] * num
    alpha[0] = 0
    for p in range(last + 1):
        best_by_rid: dict[int, float] = {}
        for u in ends_at[p]:
            a = alpha[u]
            r = rid[u]
            if a < best_by_rid.get(r, _INF):
                best_by_rid[r] = a
        if not best_by_rid:
            continue
        lefts = [(r * size, a) for r, a in best_by_rid.items()]
        by_lid: dict[int, float] = {}
        for v in starts_at[p]:
            l = lid[v]
            m = by_lid.get(l)
            if m is None:
                m = min(a + matrix[ro + l] for ro, a in lefts)
                by_lid[l] = m
            alpha[v] = m + wcost[v]
    return alpha[num - 1]


def search_cost(r: Reading) -> float:
    """読み段の探索で数えたコストの近似（編集を core.reading.SEARCH_EDIT で数え直す。重ね打ちの区別と、
    辞書の語の弧の DICT_SEARCH は無い）。"""
    return r.cost - r.breakdown.get("edit", 0.0) + sum(SEARCH_EDIT[e.kind] for e in r.edits)


def _has_raw(r: Reading) -> bool:
    """辞書に無い英字列を含むか。Reading は弧の raw を持たないので、英単語リストに無い英語区間で見分ける
    （llama.cpp のような塊も数える）。大文字で打った区間（綴りが大文字のまま。core.reading._verbatim_spans）は
    どの候補も同じに持つので数えない（数えると全候補が英字列あり扱いになり、上限と仮説の枠が効かない）。"""
    lex = lexicon()
    return any(s.kind == "en" and s.text == s.text.lower() and lex.get(s.text) is None for s in r.segs)


def preselect(rs: list[Reading], dic: Dictionary, k: int = 5) -> list[Reading]:
    """rs（readings() の戻り値。1位がコスト最小）から k 件を選んで返す。1位はそのまま先頭に置き、残りは点数の順。"""
    if len(rs) <= 1:
        return list(rs[:k])
    conv = [conv_cost(r, dic) for r in rs]
    c0 = conv[0] if conv[0] < _INF else 0.0

    def score(i: int) -> float:
        r = rs[i]
        c = conv[i] if conv[i] < _INF else c0 + 1e6
        return search_cost(r) + CONV_WEIGHT * (c - c0) + RR_LM * r.breakdown.get("lm", 0.0) + RR_EDIT * len(r.edits)

    used = [0]
    skipped = []
    n_raw = int(_has_raw(rs[0]))
    for i in sorted(range(1, len(rs)), key=score):
        if len(used) >= k:
            break
        raw = _has_raw(rs[i])
        if raw and n_raw >= RAW_QUOTA:
            skipped.append(i)
            continue
        n_raw += raw
        used.append(i)
    used += skipped[:k - len(used)]  # どの候補も英字列を含む入力（sftnoato…）では上限を超えて埋める
    # 「直さない」と「英字として読む」の仮説を1つずつ残す（core.reading._select と同じ。後ろの枠から入れ替える）
    slot = len(used) - 1
    for want in (lambda i: not rs[i].edits, lambda i: _has_raw(rs[i])):
        if slot < 1 or len(used) < k or any(want(i) for i in used):
            continue
        cand = [i for i in range(len(rs)) if want(i) and i not in used]
        if cand:
            used[slot] = min(cand, key=lambda i: rs[i].cost)
            slot -= 1
    return [rs[i] for i in used]
