"""ローマ字の打鍵列を規則でカタカナにする（`kana.py` の逆）。

DESIGN.md 方式1（規則でカタカナにして元の zenz に渡す）のベースライン用。
変換できない文字（英単語の残り・数字など）はそのまま残す。ASCII 記号は kana.py が使う対応の逆で全角にする。
"""

from core.romaji_table import MAXLEN as _MAXLEN
from core.romaji_table import TABLE as _TABLE
from core.romaji_table import VOWELS as _V


# kana.py の逆。1 文字ずつ全角へ
_SYMBOLS = {
    "-": "ー", ",": "、", ".": "。", "/": "・", "[": "「", "]": "」", "!": "！", "?": "？",
    "(": "（", ")": "）", ":": "：", "~": "〜",
}
_CONSONANTS = set("bcdfghjklmpqrstvwxyz")  # n は別扱い


def romaji_to_katakana(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if ch == "n":
            # 「n」の扱い: 母音・y が続くなら na 行。nn は常に 2 文字で「ン」（Microsoft IME・Google 日本語入力と
            # 同じ。「nna」は「ンア」、「んな」は nnna）。n' も「ン」
            if nxt == "n":
                out.append("ン")
                i += 2
                continue
            if nxt == "'":
                out.append("ン")
                i += 2
                continue
            if not nxt or (nxt not in _V and nxt != "y"):
                out.append("ン")
                i += 1
                continue

        # 促音: 同じ子音の連続、tch
        if ch in _CONSONANTS and (nxt == ch or (ch == "t" and text[i + 1:i + 3] == "ch")):
            out.append("ッ")
            i += 1
            continue

        for ln in range(min(_MAXLEN, n - i), 0, -1):
            k = _TABLE.get(text[i:i + ln])
            if k is not None:
                out.append(k)
                i += ln
                break
        else:
            out.append(_SYMBOLS.get(ch, ch))
            i += 1
    return "".join(out)
