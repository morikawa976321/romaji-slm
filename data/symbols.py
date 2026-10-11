"""出力側の記号・英数字を、英語キーボードで打つ ASCII に対応させる。"""

import unicodedata

# NFKC で ASCII にならないものだけ。全角英数・！？：（）などは NFKC が半角にする
_MAP = {
    "、": ",", "，": ",", "。": ".", "．": ".",
    "ー": "-", "ｰ": "-", "―": "-", "—": "-", "–": "-", "−": "-", "‐": "-",
    "「": "[", "」": "]", "『": "[", "』": "]", "【": "[", "】": "]", "〔": "[", "〕": "]",
    "｢": "[", "｣": "]",
    "・": "/", "･": "/",
    "〜": "~", "～": "~",
    "“": '"', "”": '"', "‘": "'", "’": "'",
}


def is_typable(s: str) -> bool:
    """入力側に置いてよい文字だけか（小文字英字・数字・空白以外の ASCII 記号）。"""
    return bool(s) and all("!" <= c <= "~" and not c.isupper() for c in s)


def to_typed(text: str) -> str | None:
    """出力側の文字列を打鍵上の文字列にする。空白は打たないので除く。対応できなければ None。"""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if ch in _MAP:
            out.append(_MAP[ch])
            continue
        n = unicodedata.normalize("NFKC", ch)
        if not n.isascii():
            return None
        out.append("".join(c for c in n.lower() if not c.isspace()))
    typed = "".join(out)
    return typed if typed == "" or is_typable(typed) else None
