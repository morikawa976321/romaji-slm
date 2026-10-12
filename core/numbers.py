"""カタカナの数の読み → 数字（Mozc の数字の書き換えに当たる部分の最小版）。

辞書は「サンビャク」→「三百」のような合成数をほとんど持たないので、読みを数として解析し、
算用数字（半角・全角）と漢数字の表記をラティスに足す。音便（イッ・ロッ・ハッ・ビャク・ゼン 等）を扱う。
日付の特殊な読み（ツイタチ・フツカ…）は「数＋日」の表記を直接返す。
"""

_DIGITS = [  # 長い読みを先に試す
    ("キュウ", 9), ("イチ", 1), ("イッ", 1), ("サン", 3), ("ヨン", 4), ("ロク", 6), ("ロッ", 6),
    ("ナナ", 7), ("シチ", 7), ("ハチ", 8), ("ハッ", 8), ("ゼロ", 0), ("レイ", 0),
    ("ニ", 2), ("ヨ", 4), ("ゴ", 5), ("ク", 9),
]
_SMALL_UNITS = [("ジュウ", 10), ("ジュッ", 10), ("ジッ", 10), ("ヒャク", 100), ("ビャク", 100), ("ピャク", 100),
                ("セン", 1000), ("ゼン", 1000)]
_BIG_UNITS = [("マン", 10_000), ("オク", 100_000_000)]

DATE_READINGS = {
    "ツイタチ": 1, "フツカ": 2, "ミッカ": 3, "ヨッカ": 4, "イツカ": 5, "ムイカ": 6, "ナノカ": 7,
    "ヨウカ": 8, "ココノカ": 9, "トオカ": 10, "ハツカ": 20,
}

_KANJI = "〇一二三四五六七八九"


def _match(s: str, i: int, table) -> list[tuple[int, int]]:
    return [(i + len(k), v) for k, v in table if s.startswith(k, i)]


def _below_10000(s: str, i: int) -> list[tuple[int, int]]:
    """位置 i から読める 1〜9999 の数（終わり位置, 値）。単位は大きい順にしか並ばない。"""
    out: list[tuple[int, int]] = []

    def go(pos: int, value: int, last_unit: int, pending_digit: int | None):
        if pending_digit is not None:
            out.append((pos, value + pending_digit))
        elif value:
            out.append((pos, value))
        for end, u in _match(s, pos, _SMALL_UNITS):
            if u >= last_unit:
                continue
            mult = 1 if pending_digit is None else pending_digit
            if pending_digit is not None and mult == 1 and u != 1000:
                continue  # 「イチジュウ」「イチヒャク」とは読まない（イッセンは読む）
            go(end, value + mult * u, u, None)
        if pending_digit is None:
            for end, d in _match(s, pos, _DIGITS):
                if d:
                    go(end, value, last_unit, d)

    go(i, 0, 10_000, None)
    return out


def number_prefixes(s: str, i: int = 0) -> list[tuple[int, int]]:
    """s[i:] の先頭から読める数（終わり位置, 値）をすべて返す。"""
    out = set(_below_10000(s, i))
    # 万・億: 1〜9999 の後に単位、さらに続きの数
    for end, v in list(out):
        for uend, u in _match(s, end, _BIG_UNITS):
            out.add((uend, v * u))
            for rend, rv in _below_10000(s, uend):
                if rv < u:
                    out.add((rend, v * u + rv))
    for end, d in _match(s, i, _DIGITS):
        if d == 0:
            out.add((end, 0))
    return sorted(out)


def kanji_numeral(n: int) -> str:
    """位取りの漢数字（三百二十五、一万二千）。"""
    if n == 0:
        return "〇"
    parts = []
    for unit, name in ((100_000_000, "億"), (10_000, "万")):
        if n >= unit:
            parts.append(kanji_numeral(n // unit) + name)
            n %= unit
    for unit, name in ((1000, "千"), (100, "百"), (10, "十")):
        if n >= unit:
            q = n // unit
            parts.append(("" if q == 1 else _KANJI[q]) + name)
            n %= unit
    if n:
        parts.append(_KANJI[n])
    return "".join(parts)


def surfaces(n: int) -> list[str]:
    half = str(n)
    full = half.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    return [half, full, kanji_numeral(n)]
