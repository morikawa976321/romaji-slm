"""QWERTY のキー配置（打鍵ミスの模擬 data/noise.py と、読みラティスの編集弧 core/reading.py で共有）。"""

ROWS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
# 行ごとの横ずれ（実際のキーボードの段差）
POS = {c: (r, i + (0.0, 0.5, 0.75, 1.25)[r]) for r, row in enumerate(ROWS) for i, c in enumerate(row)}


def neighbors(c: str) -> list[str]:
    if c not in POS:
        return []
    r, x = POS[c]
    return [d for d, (r2, x2) in POS.items() if d != c and abs(r2 - r) <= 1 and abs(x2 - x) <= 1.0]


NEIGHBORS = {c: frozenset(neighbors(c)) for c in POS}
