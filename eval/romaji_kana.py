"""ローマ字の打鍵列を規則でカタカナにする（`kana.py` の逆）。

DESIGN.md 方式1（規則でカタカナにして元の zenz に渡す）のベースライン用。
変換できない文字（英単語の残り・数字など）はそのまま残す。ASCII 記号は kana.py が使う対応の逆で全角にする。
"""

_V = "aiueo"
_KANA_V = {"a": "ア", "i": "イ", "u": "ウ", "e": "エ", "o": "オ"}
_SMALL_V = {"a": "ァ", "i": "ィ", "u": "ゥ", "e": "ェ", "o": "ォ"}

# 行ごとの基本形（あ段〜お段）。None は存在しない綴り
_ROWS = {
    "k": "カキクケコ", "s": "サシスセソ", "t": "タチツテト", "n": "ナニヌネノ", "h": "ハヒフヘホ",
    "m": "マミムメモ", "r": "ラリルレロ", "g": "ガギグゲゴ", "z": "ザジズゼゾ", "d": "ダヂヅデド",
    "b": "バビブベボ", "p": "パピプペポ", "v": "ヴァヴィヴヴェヴォ", "f": "ファフィフフェフォ",
    "j": "ジャジジュジェジョ", "c": "カシクセコ",
}
# 拗音の頭の子音 → 「い段」のカナ（+ ャュェョ）
_YOUON_BASE = {
    "k": "キ", "g": "ギ", "s": "シ", "z": "ジ", "j": "ジ", "t": "チ", "c": "チ", "d": "ヂ",
    "n": "ニ", "h": "ヒ", "b": "ビ", "p": "ピ", "m": "ミ", "r": "リ", "f": "フ", "v": "ヴ",
}
_SMALL_Y = {"a": "ャ", "u": "ュ", "e": "ェ", "o": "ョ", "i": "ィ"}


def _build() -> dict[str, str]:
    t: dict[str, str] = {}
    for v in _V:
        t[v] = _KANA_V[v]
        t["x" + v] = t["l" + v] = _SMALL_V[v]
    t["wa"], t["wi"], t["wu"], t["we"], t["wo"] = "ワ", "ウィ", "ウ", "ウェ", "ヲ"
    t["ya"], t["yu"], t["yo"], t["ye"], t["yi"] = "ヤ", "ユ", "ヨ", "イェ", "イ"
    for c, kana in _ROWS.items():
        # v 行と f 行・j 行は 2 文字カナを含むので、母音ごとに切り出す
        if len(kana) == 5:
            parts = list(kana)
        elif c == "v":
            parts = ["ヴァ", "ヴィ", "ヴ", "ヴェ", "ヴォ"]
        elif c == "f":
            parts = ["ファ", "フィ", "フ", "フェ", "フォ"]
        else:  # j
            parts = ["ジャ", "ジ", "ジュ", "ジェ", "ジョ"]
        for v, k in zip(_V, parts):
            t[c + v] = k
    # ワープロ式の揺れ・特殊な綴り
    t.update({
        "shi": "シ", "sha": "シャ", "shu": "シュ", "she": "シェ", "sho": "ショ",
        "chi": "チ", "cha": "チャ", "chu": "チュ", "che": "チェ", "cho": "チョ",
        "tsu": "ツ", "tsa": "ツァ", "tsi": "ツィ", "tse": "ツェ", "tso": "ツォ",
        "ji": "ジ", "fu": "フ", "hu": "フ", "si": "シ", "ti": "チ", "tu": "ツ", "zi": "ジ", "di": "ヂ", "du": "ヅ",
        "ca": "カ", "ci": "シ", "cu": "ク", "ce": "セ", "co": "コ",
        "kwa": "クァ", "kwi": "クィ", "kwu": "クゥ", "kwe": "クェ", "kwo": "クォ",
        "qa": "クァ", "qi": "クィ", "qu": "ク", "qe": "クェ", "qo": "クォ",
        "gwa": "グァ", "gwi": "グィ", "gwu": "グゥ", "gwe": "グェ", "gwo": "グォ",
        "tha": "テァ", "thi": "ティ", "thu": "テュ", "the": "テェ", "tho": "テョ",
        "dha": "デァ", "dhi": "ディ", "dhu": "デュ", "dhe": "デェ", "dho": "デョ",
        "twa": "トァ", "twi": "トィ", "twu": "トゥ", "twe": "トェ", "two": "トォ",
        "dwa": "ドァ", "dwi": "ドィ", "dwu": "ドゥ", "dwe": "ドェ", "dwo": "ドォ",
        "wha": "ウァ", "whi": "ウィ", "whu": "ウ", "whe": "ウェ", "who": "ウォ",
        "xtu": "ッ", "ltu": "ッ", "xtsu": "ッ", "ltsu": "ッ",
        "xwa": "ヮ", "lwa": "ヮ", "xka": "ヵ", "xke": "ヶ", "lka": "ヵ", "lke": "ヶ",
        "xya": "ャ", "xyu": "ュ", "xyo": "ョ", "lya": "ャ", "lyu": "ュ", "lyo": "ョ",
        "xn": "ン",
    })
    # 拗音: 子音 + y + 母音、および sh/ch + 母音以外の i 段
    for c, base in _YOUON_BASE.items():
        for v, s in _SMALL_Y.items():
            t.setdefault(c + "y" + v, base + s)
    t.update({"jya": "ジャ", "jyu": "ジュ", "jyo": "ジョ", "jye": "ジェ", "jyi": "ジィ",
              "tya": "チャ", "tyu": "チュ", "tyo": "チョ", "cya": "チャ", "cyu": "チュ", "cyo": "チョ",
              "fya": "フャ", "fyu": "フュ", "fyo": "フョ"})
    return t


_TABLE = _build()
_MAXLEN = max(map(len, _TABLE))

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
