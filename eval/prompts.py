"""モデルに渡すプロンプトの雛形。

zenz-v3 の形式は https://github.com/ensan-hcl/AzooKeyKanaKanjiConverter/blob/main/Docs/zenzai.md の
用語欄による: `\\uEE02<context>\\uEE00<input_katakana>\\uEE01<output></s>`（左文脈を前置）。
モデルは `\\uEE01` の直後から出力を生成し、`</s>`（EOS）で止まる。
"""

import unicodedata
from dataclasses import dataclass
from typing import Callable

from .romaji_kana import romaji_to_katakana

# zenz の特殊文字（私用領域）
INPUT = ""    # 入力（カタカナ読み）の開始
OUTPUT = ""   # 出力の開始（生成の起点）
CONTEXT = ""  # 左文脈の開始
# ローマ字の打鍵列の開始（このリポジトリで足した印。zenz の語彙に無い私用領域の字で、UTF-8 の3バイトに分かれる）
ROMAJI = "\uEE10"

# romaji2ja 配布元 code/normalization.py と同じ規則（NFKC・小文字化のうえ、空白とこの記号群を捨てる）
_SOFT_SEP = frozenset("-_'`\\/|.,;:!?()[]{}<>\"‐‑‒–—―−")


def normalize_like_romaji2ja(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.strip()).lower()
    return "".join(
        ch for ch in text
        if not (ch.isspace() or ch in _SOFT_SEP or unicodedata.category(ch) == "Cf")
    )


@dataclass(frozen=True)
class Template:
    name: str
    render: Callable[[str, str], str]  # (romaji, left_context) -> プロンプト
    description: str


def _zenz(source: Callable[[str], str]) -> Callable[[str, str], str]:
    # 左文脈が空のときは CONTEXT ごと省く（v1 形式。v3 でも空文脈はこの形のはずだが、
    # 公式仕様に明記はない。実機での確認が要る）
    def render(romaji: str, left_context: str) -> str:
        head = CONTEXT + left_context if left_context else ""
        return f"{head}{INPUT}{source(romaji)}{OUTPUT}"
    return render


def _reading(romaji: str, left_context: str) -> str:
    head = CONTEXT + left_context if left_context else ""
    return f"{head}{ROMAJI}{romaji}{INPUT}"


TEMPLATES: dict[str, Template] = {t.name: t for t in [
    Template("romaji2ja", lambda r, c: r, "romaji2ja 形式 `{romaji}`（左文脈は使わない）"),
    Template("romaji2ja-normalized", lambda r, c: normalize_like_romaji2ja(r),
             "romaji2ja の配布元の正規化（空白と記号を捨てる）を通した `{romaji}`"),
    Template("zenz-v3", _zenz(romaji_to_katakana),
             "zenz-v3 形式 `EE02{left_context}EE00{kana}EE01`。ローマ字は規則でカタカナにして渡す"),
    Template("zenz-v3-romaji", _zenz(lambda r: r),
             "ローマ字版 zenz-v3 形式 `EE02{left_context}EE00{romaji}EE01`。微調整後のモデル用"),
    Template("zenz-v3-romaji-reading", _reading,
             "二段形式 `EE02{left_context}EE10{romaji}EE00`。モデルが読み→EE01→出力の順に書く。"
             "読みより後ろは元の zenz-v3 形式そのもの"),
]}

# 雛形のうち、生成がまず読みを書き、OUTPUT の後ろだけが変換結果になるもの
TWO_STAGE = {"zenz-v3-romaji-reading"}


def split_two_stage(generated: str) -> tuple[str, str]:
    """二段形式の生成を (読み, 変換結果) に分ける。OUTPUT が出なければ全体を変換結果とみなす。"""
    reading, sep, text = generated.partition(OUTPUT)
    return (reading, text) if sep else ("", generated)


def build_prompt(template: str, romaji: str, left_context: str = "") -> str:
    try:
        t = TEMPLATES[template]
    except KeyError:
        raise ValueError(f"未知の雛形: {template}（選択肢: {', '.join(TEMPLATES)}）") from None
    return t.render(romaji, left_context)
