"""Mozc の OSS 辞書（src/data/dictionary_oss/）を models/mozc/ に取得する。

    uv run python -m core.fetch_mozc

データ本体はコミットしない（models/ は .gitignore）。版は下の MOZC_COMMIT で固定する。
出典とライセンスは docs/KANAKANJI.md。
"""

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOZC_DIR = ROOT / "models" / "mozc"
MOZC_COMMIT = "18e76511af9371a9a4409b1d13c3e30884b53e9c"  # google/mozc master、2026-10-12 取得
BASE_URL = f"https://raw.githubusercontent.com/google/mozc/{MOZC_COMMIT}/src/data/dictionary_oss/"
FILES = [
    *(f"dictionary{i:02d}.txt" for i in range(10)),
    "connection_single_column.txt",
    "id.def",
    "README.txt",  # IPAdic・沖縄辞書のライセンス文。配布するときは同梱する
]


def fetch(dest: Path = MOZC_DIR, force: bool = False) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = dest / name
        if path.exists() and not force:
            continue
        print(f"取得: {name}", file=sys.stderr)
        with urllib.request.urlopen(BASE_URL + name, timeout=120) as r:
            data = r.read()
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    (dest / "VERSION").write_text(f"google/mozc {MOZC_COMMIT}\n", encoding="utf-8")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=MOZC_DIR)
    ap.add_argument("--force", action="store_true", help="既にあっても取り直す")
    args = ap.parse_args()
    print(fetch(args.dest, args.force))


if __name__ == "__main__":
    main()
