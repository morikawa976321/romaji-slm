"""評価指標: 完全一致・ゆるい一致・MinCER。参照は複数あってよい（どれか1つに合えば正解）。"""

import unicodedata

# ゆるい一致で無視する文字: 句読点・記号(P*)、空白(Z*)、制御・書式(C*)、長音「ー」、チルダ類
_EXTRA_IGNORED = frozenset("ーｰ~〜～")


def loose_normalize(s: str) -> str:
    """記号・長音・空白を捨て、NFKC のうえ英字を小文字にする。"""
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(
        ch for ch in s
        if ch not in _EXTRA_IGNORED and unicodedata.category(ch)[0] not in "PZC"
    )


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    return levenshtein(ref, hyp) / max(len(ref), 1)


def min_cer(refs: list[str], hyp: str) -> float:
    return min(cer(r, hyp) for r in refs)


def score(refs: list[str], hyp: str) -> dict:
    return {
        "exact": hyp in refs,
        "loose": loose_normalize(hyp) in {loose_normalize(r) for r in refs},
        "min_cer": min_cer(refs, hyp),
    }
