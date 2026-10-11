"""文字単位トークナイザの語彙調査: ASCII 文字が単独トークンを持つか、UNK 扱いになるか。

    uv run python -m eval.tokenizer_survey --model Miwa-Keita/zenz-v3.1-small

各文字について、(1) 語彙に単独で載っているか、(2) エンコードで UNK になるか、(3) 往復（encode→decode）で戻るか、を表にする。
"""

import argparse
import string
import sys

from .prompts import CONTEXT, INPUT, OUTPUT


def survey(tok, chars: str) -> list[dict]:
    vocab = tok.get_vocab()
    unk = tok.unk_token_id
    rows = []
    for ch in chars:
        ids = tok(ch, add_special_tokens=False).input_ids
        rows.append({
            "char": ch,
            "in_vocab": ch in vocab,
            "ids": ids,
            "unk": unk is not None and unk in ids,
            "n_tokens": len(ids),
            "roundtrip": tok.decode(ids, skip_special_tokens=True).replace(" ", "") == ch if ids else False,
        })
    return rows


def render(rows: list[dict], title: str) -> str:
    lines = [f"### {title}", "", "| 文字 | 語彙に単独である | UNK になる | エンコード後トークン数 | 往復で戻る |", "|---|---|---|---|---|"]
    for r in rows:
        shown = "`" + r["char"] + "`" if r["char"] not in "|`" else {"|": "`\\|`", "`": "`` ` ``"}[r["char"]]
        lines.append(f"| {shown} | {'○' if r['in_vocab'] else '×'} | {'UNK' if r['unk'] else '-'} "
                     f"| {r['n_tokens']} | {'○' if r['roundtrip'] else '×'} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from transformers import AutoTokenizer

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Miwa-Keita/zenz-v3.1-small")
    args = ap.parse_args(argv)
    tok = AutoTokenizer.from_pretrained(args.model)
    print(f"tokenizer={type(tok).__name__} vocab_size={len(tok)} unk={tok.unk_token!r} eos={tok.eos_token!r} "
          f"bos={tok.bos_token!r} pad={tok.pad_token!r}\n")
    groups = {
        "ラテン小文字 a-z": string.ascii_lowercase,
        "数字 0-9": string.digits,
        "ASCII 記号": "".join(c for c in string.punctuation) + " ",
        "ラテン大文字 A-Z（入力には現れないが参考）": string.ascii_uppercase,
        "zenz の特殊文字": INPUT + OUTPUT + CONTEXT + "".join(chr(0xEE00 + i) for i in range(3, 8)),
    }
    for title, chars in groups.items():
        print(render(survey(tok, chars), title))
    sample = CONTEXT + "私の名前は" + INPUT + "ワタシ" + OUTPUT
    ids = tok(sample, add_special_tokens=False).input_ids
    print("プロンプト例（add_special_tokens=False）:", ids, tok.convert_ids_to_tokens(ids))
    print("プロンプト例（add_special_tokens=True）:", tok(sample).input_ids)


if __name__ == "__main__":
    main()
