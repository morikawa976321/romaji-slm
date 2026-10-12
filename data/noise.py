"""QWERTY 上の打鍵ミスを模したノイズ。"""

import random

from core.keyboard import POS as _POS
from core.keyboard import neighbors as _neighbors

_OPS = (("sub", .35), ("drop", .25), ("insert", .2), ("swap", .2))


def _apply_one(s: str, rng: random.Random) -> tuple[str, str] | None:
    """1か所にノイズを入れる。入れられなければ None。記号の位置は変えない。"""
    idx = [i for i, c in enumerate(s) if c in _POS]
    if not idx:
        return None
    op = rng.choices([o for o, _ in _OPS], [w for _, w in _OPS])[0]
    i = rng.choice(idx)
    c = s[i]
    if op == "sub":
        t = rng.choice(_neighbors(c))
        return s[:i] + t + s[i + 1:], f"sub:{i}:{c}>{t}"
    if op == "drop":
        return s[:i] + s[i + 1:], f"drop:{i}:{c}"
    if op == "insert":
        # 直前の字を押しすぎる（重複）か、隣接キーを余計に押す
        t = c if rng.random() < 0.5 else rng.choice(_neighbors(c))
        return s[:i] + t + s[i:], f"insert:{i}:{t}"
    if i + 1 < len(s) and s[i + 1] in _POS and s[i + 1] != c:
        return s[:i] + s[i + 1] + c + s[i + 2:], f"swap:{i}:{c}{s[i + 1]}"
    return None


def add_noise(s: str, rng: random.Random, rate: float = 0.3, max_ops: int = 2) -> tuple[str, list[str]]:
    """確率 rate で 1〜max_ops 個のノイズを入れる。(文字列, 記録) を返す。短すぎる入力には入れない。"""
    if len(s) < 3 or rng.random() >= rate:
        return s, []
    n = rng.randint(1, max_ops)
    notes: list[str] = []
    cur = s
    for _ in range(n):
        for _try in range(5):
            r = _apply_one(cur, rng)
            if r:
                cur, note = r
                notes.append(note)
                break
    if cur == s or not cur:
        return s, []
    return cur, notes
