"""変換コアの部品どうしの受け渡し形式（docs/REDESIGN.md「推奨構成」）。

    打鍵列 ─(core.reading)→ Reading の N-best ─(core.kanakanji)→ Conversion の N-best ─(core.select)→ 1候補

部品を別々に作って挿し替えられるよう、境目はこの型だけにする。位置はすべて、利用者が打った打鍵列
（正規化前の文字列）上の文字位置で、[start, end) の半開区間。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Edit:
    """読みを得るために打鍵列に施した修正1つ（打鍵ミスの吸収）。"""

    kind: str  # "sub"（置換）| "drop"（脱字を補う）| "insert"（余計な1字を捨てる）| "swap"（隣接2字の入れ替え）
    pos: int  # 打鍵列上の位置
    detail: str  # 例: "q>a"（q を a に直した）、"+u"（u を補った）、"-k"（k を捨てた）、"ot>to"


@dataclass(frozen=True)
class Seg:
    """読みの1区間。"""

    kind: str  # "kana" | "en" | "num" | "sym"
    start: int
    end: int
    text: str  # kana: カタカナの読み / en: 小文字の綴り / num: 数字列（半角）/ sym: 打った ASCII 記号
    # en: 表記の候補（辞書の登録表記があれば先頭、続けて綴りのまま）。sym: 出力側の記号の候補（例: "," → ("、", ",")）。
    # kana と num では空。
    surfaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reading:
    """前段（打鍵列 → 読み）の候補1つ。"""

    segs: tuple[Seg, ...]
    edits: tuple[Edit, ...] = ()
    cost: float = 0.0  # 前段のコスト（小さいほど良い）。編集・英語区間・n/nn の揺れなどの合計
    breakdown: dict = field(default_factory=dict, compare=False, hash=False)  # コストの内訳（表示用）

    def zenz_input(self) -> str:
        """zenz の input と同じ形の読み: かなはカタカナ、英字・数字は綴りのまま、記号は出力側の第1候補。"""
        return "".join(s.surfaces[0] if s.kind == "sym" and s.surfaces else s.text for s in self.segs)


@dataclass(frozen=True)
class Word:
    """変換結果の1語。表層と読みの対応を必ず持つ（安定性の保証: 読みで説明できない表層は作らない）。"""

    surface: str
    reading: str  # kana 区間ならカタカナ、それ以外は Seg.text
    seg_index: int  # Reading.segs の何番目の区間に属するか（kana 区間は複数語に分かれうる）
    source: str  # "dict"（辞書の語）| "kana"（辞書に無くかなのまま）| "en" | "num" | "sym"
    cost: float = 0.0  # 語のコスト（辞書の生起コストなど）
    lid: int = -1  # 接続に使った品詞 ID（Mozc の id.def。英語・数字・記号・かなのままの語にも、接続に使った ID を入れる）
    rid: int = -1


@dataclass(frozen=True)
class Conversion:
    """後段（読み → 表記）の候補1つ。"""

    reading: Reading
    words: tuple[Word, ...]
    cost: float = 0.0  # 辞書・接続コストの合計（Reading.cost は含まない）

    @property
    def surface(self) -> str:
        return "".join(w.surface for w in self.words)
