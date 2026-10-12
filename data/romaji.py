"""カタカナ読み → ワープロ式ローマ字。表記揺れを乱数で混ぜる。

乱数 rng=None のときは揺れなしの標準綴り（eval.kana.kata_to_romaji に近い）を返す。
"""

import random
from collections.abc import Sequence

from eval.kana import _BASE, _DIGRAPH

_SMALL = "ァィゥェォャュョ"

# 標準綴りが先頭。重みは「実際によく使われる打ち方」の目安で、実測ではない
_VARIANTS: dict[str, list[tuple[str, float]]] = {
    "シ": [("shi", .7), ("si", .3)],
    "チ": [("chi", .65), ("ti", .35)],
    "ツ": [("tsu", .75), ("tu", .25)],
    "フ": [("fu", .7), ("hu", .3)],
    "ジ": [("ji", .65), ("zi", .35)],
    "シャ": [("sha", .65), ("sya", .35)],
    "シュ": [("shu", .65), ("syu", .35)],
    "ショ": [("sho", .65), ("syo", .35)],
    "シェ": [("she", .7), ("sye", .3)],
    "チャ": [("cha", .6), ("tya", .3), ("cya", .1)],
    "チュ": [("chu", .6), ("tyu", .3), ("cyu", .1)],
    "チョ": [("cho", .6), ("tyo", .3), ("cyo", .1)],
    "チェ": [("che", .7), ("tye", .3)],
    "ジャ": [("ja", .55), ("zya", .3), ("jya", .15)],
    "ジュ": [("ju", .55), ("zyu", .3), ("jyu", .15)],
    "ジョ": [("jo", .55), ("zyo", .3), ("jyo", .15)],
    "ジェ": [("je", .7), ("zye", .3)],
    "ファ": [("fa", .8), ("hwa", .2)],
    "フィ": [("fi", .8), ("hwi", .2)],
    "フェ": [("fe", .8), ("hwe", .2)],
    "フォ": [("fo", .8), ("hwo", .2)],
}

_P_SPLIT_SMALL = 0.04  # キャ → kixya のように小書き文字を別打ちする
_P_XTU = 0.03  # 促音を子音重ねでなく xtu/ltu で打つ
_P_L = 0.2  # 小書きの x を l にする
_P_SHORT_N = 0.7  # 「ん」を n 1つで打てるときに n にする割合

_KATA_FIX = {"ヵ": "カ", "ヶ": "ケ"}


class UnreadableKana(ValueError):
    pass


def _to_kata(text: str) -> str:
    out = []
    for ch in text:
        if "ぁ" <= ch <= "ゖ":
            ch = chr(ord(ch) + 0x60)
        out.append(_KATA_FIX.get(ch, ch))
    return "".join(out)


def _pick(rng: random.Random | None, options: Sequence[tuple[str, float]]) -> str:
    if rng is None or len(options) == 1:
        return options[0][0]
    return rng.choices([o for o, _ in options], [w for _, w in options])[0]


def _small_prefix(rom: str, rng: random.Random | None) -> str:
    if rng is not None and rom.startswith("x") and rng.random() < _P_L:
        return "l" + rom[1:]
    return rom


def _mora_romaji(kana: str, rng: random.Random | None) -> str:
    if len(kana) == 2 and kana in _DIGRAPH:
        first, second = kana
        if rng is not None and second in _SMALL and first in _BASE and rng.random() < _P_SPLIT_SMALL:
            return _BASE[first] + _small_prefix(_BASE[second], rng)
        return _pick(rng, _VARIANTS.get(kana) or [(_DIGRAPH[kana], 1)])
    rom = _pick(rng, _VARIANTS.get(kana) or [(_BASE[kana], 1)])
    return _small_prefix(rom, rng) if kana in _SMALL else rom


def romanize(pieces: Sequence[tuple[str, str]], rng: random.Random | None = None) -> str:
    """pieces: ("kana", カタカナ読み) と ("lit", そのまま打つ文字列) の列。"""
    units: list[tuple[str, str]] = []  # (種別 m|sok|n|lit, カナまたは打鍵)
    for kind, text in pieces:
        if kind == "lit":
            units.append(("lit", text))
            continue
        text = _to_kata(text)
        i = 0
        while i < len(text):
            if text[i:i + 2] in _DIGRAPH:
                units.append(("m", text[i:i + 2]))
                i += 2
                continue
            ch = text[i]
            if ch == "ッ":
                units.append(("sok", ch))
            elif ch == "ン":
                units.append(("n", ch))
            elif ch in _BASE and ch not in "、。・「」！？（）：〜":
                units.append(("m", ch))
            else:
                raise UnreadableKana(f"読みに打てない文字がある: {ch!r} in {text!r}")
            i += 1

    out = [""] * len(units)
    for i in range(len(units) - 1, -1, -1):
        kind, text = units[i]
        nxt = out[i + 1] if i + 1 < len(units) else ""
        nxt_kind = units[i + 1][0] if i + 1 < len(units) else ""
        if kind == "lit":
            out[i] = text
        elif kind == "m":
            out[i] = _mora_romaji(text, rng)
        elif kind == "sok":
            doubles = nxt_kind == "m" and nxt[0] not in "aiueoxl"
            if doubles and not (rng is not None and rng.random() < _P_XTU):
                out[i] = nxt[0]
            else:
                out[i] = "ltu" if rng is not None and rng.random() < _P_L else "xtu"
        else:  # n
            if nxt_kind in ("m", "sok") and nxt[0] not in "aiueoyn":
                out[i] = "n" if rng is None or rng.random() < _P_SHORT_N else "nn"
            else:
                out[i] = "nn"
    return "".join(out)


def kana_to_romaji(kana: str, rng: random.Random | None = None) -> str:
    return romanize([("kana", kana)], rng)
