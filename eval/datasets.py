"""評価セットの読み込み。

共有 JSONL 形式（学習データ生成側と同じ）:
    {"romaji": str, "output": str, "left_context": str, "meta": {...}}
`meta.alternatives`（文字列のリスト）があれば、それらも正解に数える。`meta.category` はカテゴリ別集計に使う。
`left_context` と `meta` は省略可。`meta.pair_of` は対になる正常入力の id（打鍵ミス版の文に付ける）。
"""

import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .kana import kata_to_romaji

ROOT = Path(__file__).resolve().parent.parent
AJIMEE_URL = ("https://raw.githubusercontent.com/ensan-hcl/AJIMEE-Bench/main/"
              "JWTD_v2/v1/evaluation_items.json")
AJIMEE_PATH = ROOT / "models" / "ajimee" / "evaluation_items.json"
SETS = ROOT / "eval" / "sets"
USER_STYLE_PATH = SETS / "user_style.jsonl"
# gen_test を dev（閾値・重みの調整用）と val（最終確認用）に分けたもの。eval/split_gen.py が作る
GEN_SETS = ("gen_test", "gen_dev", "gen_val")


@dataclass
class Item:
    id: str
    romaji: str
    refs: list[str]
    left_context: str = ""
    category: str = "all"
    meta: dict = field(default_factory=dict)

    @property
    def pair_of(self) -> str | None:
        return self.meta.get("pair_of")


def download_ajimee(path: Path = AJIMEE_PATH) -> Path:
    """AJIMEE-Bench（データ CC BY-SA 3.0）を取得する。データ本体はコミットしない（models/ は .gitignore）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(AJIMEE_URL, timeout=60) as r:
        path.write_bytes(r.read())
    return path


def load_jsonl(path: Path | str, prefix: str = "") -> list[Item]:
    items = []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(l for l in lines if l.strip()):
        d = json.loads(line)
        meta = d.get("meta") or {}
        # 正解の無い行（読み段のセット typo_hard など）は refs が空。実行系どうしの出力の突き合わせ（eval.parity）にだけ使う
        refs = [d["output"], *meta.get("alternatives", [])] if "output" in d else []
        items.append(Item(
            id=f"{prefix}{i}", romaji=d["romaji"], refs=refs,
            left_context=d.get("left_context") or "", category=meta.get("category", "all"), meta=meta,
        ))
    return items


def load_ajimee(use_context: bool, path: Path = AJIMEE_PATH) -> list[Item]:
    """カタカナ input を kana.py でローマ字にした AJIMEE。use_context なら context_text を左文脈に渡す。

    カテゴリは元の項目が文脈を持つか（with_context / without_context）で、use_context に依らず同じ。
    """
    if not path.exists():
        download_ajimee(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Item(
            id=str(it["index"]), romaji=kata_to_romaji(it["input"]), refs=list(it["expected_output"]),
            left_context=it["context_text"] if use_context else "",
            category="with_context" if it["context_text"] else "without_context",
            meta={"kana": it["input"]},
        )
        for it in raw
    ]


_WAC_SURF = re.compile(r"[ぁ-ゖァ-ヺー一-鿿々、。「」・]+")
_WAC_KANA = re.compile(r"[ァ-ヴー、。「」・]+")


def load_wac_test(limit: int = 200) -> list[Item]:
    """京都大学 Wikipedia 注釈コーパスの test（読みの言語モデルの学習に使っていない）から、かな・漢字・句読点だけの
    10〜45 字の文。正解は原文の表層、打鍵列は人手の読みをローマ字にしたもの。初回にコーパスを取りに行く
    （models/wac。データはコミットしない）。読み段の調整に使った wac-test（eval/reading_bench.py）とは別で、漢字を含む。"""
    from core.kana_lm import to_katakana, wac_sentences

    items = []
    for sent in wac_sentences(("test",)):
        surf = "".join(s for s, _ in sent)
        kana = "".join(s if s in "、。「」・" else to_katakana(r) for s, r in sent)
        if not (10 <= len(surf) <= 45 and _WAC_SURF.fullmatch(surf) and _WAC_KANA.fullmatch(kana)):
            continue
        items.append(Item(id=str(len(items)), romaji=kata_to_romaji(kana), refs=[surf], category="wac",
                          meta={"kana": kana}))
    return items[:limit]


def load_set(name: str, limit: int | None = None) -> list[Item]:
    """name: ajimee（左文脈あり）/ ajimee-noctx / user_style / gen_test / gen_dev / gen_val / file:<jsonl のパス>"""
    if name == "ajimee":
        items = load_ajimee(True)
    elif name == "ajimee-noctx":
        items = load_ajimee(False)
    elif name == "user_style":
        items = load_jsonl(USER_STYLE_PATH)
    elif name == "wac_test":
        items = load_wac_test()
    elif name in GEN_SETS:
        items = load_jsonl(SETS / f"{name}.jsonl")
    elif name.startswith("file:"):
        items = load_jsonl(name[5:])
    else:
        raise ValueError(f"未知のセット: {name}（ajimee / ajimee-noctx / user_style / gen_test / gen_dev / gen_val / file:<path>）")
    return items[:limit] if limit else items
