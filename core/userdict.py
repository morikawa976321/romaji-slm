"""本人のユーザー辞書（docs/REVIEW.md）。利用履歴の振り返りで AI が誤変換から作り、変換器が起動時に読む。

置き場所: 環境変数 ROMAJI_SLM_USER_DICT、無ければ %LOCALAPPDATA%\\romaji-slm\\user_dict.tsv（無くてもよい）。
本人が打った文そのものはここに書かない（語・綴りの対だけ。履歴は %LOCALAPPDATA% の history.jsonl に残る）。

1 行 1 項目、タブ区切り。# で始まる行と空行は無視する。

    en    <綴り>  [<表記>]  [<加点>]   英字のまま使う語（supabase、vite）。英語の弧に入れる。加点（既定 0）は
                                        英語の弧のコストから引く値で、日本語の読みと紛れる語（main、node）の
                                        向きを本人寄りにする。大きくしすぎると「ので」が node になる（上限 1.5）
    word  <読み>  <表記>  [<コスト>]   かな漢字辞書に足す語。読みはひらがなかカタカナ。コストは Mozc の尺度
                                        （既定 3500。小さいほど出やすい。一般的な名詞は 3000〜6000）
    fix   <打鍵>  <意図>               本人の打ち間違いの癖（denatu → dennatu）。打鍵列に <打鍵> を含むとき、
                                        <意図> に直した読みも候補に足す（直すかどうかは採点が決める）
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV = "ROMAJI_SLM_USER_DICT"
EN_BONUS_MAX = 1.5
WORD_COST_DEFAULT = 3500


@dataclass
class UserDict:
    en: dict[str, tuple[str, float]] = field(default_factory=dict)  # 綴り → (表記, 加点)
    words: list[tuple[str, str, int]] = field(default_factory=list)  # (読み, 表記, コスト)
    fixes: list[tuple[str, str]] = field(default_factory=list)  # (打鍵, 意図)
    errors: list[str] = field(default_factory=list)  # 読めなかった行（落とさずに知らせる）

    def __bool__(self) -> bool:
        return bool(self.en or self.words or self.fixes)


def default_path() -> Path:
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "romaji-slm" / "user_dict.tsv"


def parse(text: str) -> UserDict:
    d = UserDict()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        kind = cols[0]
        try:
            if kind == "en" and len(cols) >= 2 and cols[1]:
                spelling = cols[1].lower()
                surface = cols[2] if len(cols) >= 3 and cols[2] else spelling
                bonus = min(EN_BONUS_MAX, max(0.0, float(cols[3]))) if len(cols) >= 4 and cols[3] else 0.0
                d.en[spelling] = (surface, bonus)
            elif kind == "word" and len(cols) >= 3 and cols[1] and cols[2]:
                cost = int(cols[3]) if len(cols) >= 4 and cols[3] else WORD_COST_DEFAULT
                d.words.append((cols[1], cols[2], cost))
            elif kind == "fix" and len(cols) >= 3 and cols[1] and cols[2] and cols[1] != cols[2]:
                d.fixes.append((cols[1].lower(), cols[2].lower()))
            else:
                d.errors.append(f"{n}: {raw}")
        except ValueError:
            d.errors.append(f"{n}: {raw}")
    return d


def load(path: Path | str | None = None) -> UserDict:
    p = Path(path) if path else default_path()
    if not p.exists():
        return UserDict()
    return parse(p.read_text(encoding="utf-8"))
