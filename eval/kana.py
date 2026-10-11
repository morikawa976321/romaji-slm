"""カタカナ読みをワープロ式ローマ字（キーボードで打つ綴り）に変換する。

AJIMEE-Bench の入力はカタカナ読みなので、ローマ字入力の評価に流用するためにこれで打鍵列へ戻す。
"""

_BASE = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヲ": "wo",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "ダ": "da", "ヂ": "di", "ヅ": "du", "デ": "de", "ド": "do",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "ヴ": "vu",
    "ァ": "xa", "ィ": "xi", "ゥ": "xu", "ェ": "xe", "ォ": "xo",
    "ャ": "xya", "ュ": "xyu", "ョ": "xyo", "ヮ": "xwa",
    "ー": "-", "、": ",", "。": ".", "・": "/", "「": "[", "」": "]",
    "！": "!", "？": "?", "（": "(", "）": ")", "：": ":", "〜": "~",
}

_DIGRAPH = {
    "キャ": "kya", "キュ": "kyu", "キョ": "kyo", "シャ": "sha", "シュ": "shu", "ショ": "sho",
    "チャ": "cha", "チュ": "chu", "チョ": "cho", "ニャ": "nya", "ニュ": "nyu", "ニョ": "nyo",
    "ヒャ": "hya", "ヒュ": "hyu", "ヒョ": "hyo", "ミャ": "mya", "ミュ": "myu", "ミョ": "myo",
    "リャ": "rya", "リュ": "ryu", "リョ": "ryo", "ギャ": "gya", "ギュ": "gyu", "ギョ": "gyo",
    "ジャ": "ja", "ジュ": "ju", "ジョ": "jo", "ビャ": "bya", "ビュ": "byu", "ビョ": "byo",
    "ピャ": "pya", "ピュ": "pyu", "ピョ": "pyo", "ヂャ": "dya", "ヂュ": "dyu", "ヂョ": "dyo",
    "シェ": "she", "ジェ": "je", "チェ": "che", "ティ": "thi", "ディ": "dhi", "デュ": "dhu",
    "トゥ": "twu", "ドゥ": "dwu", "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo",
    "ウィ": "wi", "ウェ": "we", "ウォ": "who", "ヴァ": "va", "ヴィ": "vi", "ヴェ": "ve", "ヴォ": "vo",
    "ツァ": "tsa", "ツェ": "tse", "ツォ": "tso", "イェ": "ye", "クァ": "kwa", "グァ": "gwa",
}


def kata_to_romaji(text: str) -> str:
    syllables: list[str] = []
    i = 0
    while i < len(text):
        two = text[i:i + 2]
        if two in _DIGRAPH:
            syllables.append(_DIGRAPH[two])
            i += 2
            continue
        ch = text[i]
        if "ぁ" <= ch <= "ゖ":
            ch = chr(ord(ch) + 0x60)
        syllables.append(_BASE.get(ch, ch) if ch not in "ッン" else ch)
        i += 1

    out: list[str] = []
    for j, s in enumerate(syllables):
        nxt = syllables[j + 1] if j + 1 < len(syllables) else ""
        if s == "ッ":
            # 次の音節の子音を重ねる。子音で始まらない（記号・末尾）ときは単独の小さい「っ」
            out.append(nxt[0] if nxt[:1].isalpha() and nxt[0] not in "aiueon" else "xtu")
        elif s == "ン":
            # 母音・y が続くと「ん」が次と結合するので n' と打つ（nn+母音は IME では「ん」+な行に読まれ、
            # 「ンヤ」が honnya=ほんにゃ になる）。n が続くときは n だけで足りる（nna=んな）。末尾は nn
            out.append("nn" if not nxt else "n'" if nxt[0] in "aiueoy" else "n")
        else:
            out.append(s)
    return "".join(out)
