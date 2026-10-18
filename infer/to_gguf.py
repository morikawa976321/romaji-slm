"""HF 形式の微調整済みモデル → GGUF（f16・Q8_0・Q5_K_M）。

  python -m infer.to_gguf models/zenz-romaji-v1 --llama-cpp <llama.cpp のチェックアウト> [--check]

llama.cpp の convert_hf_to_gguf.py を使う。zenz のトークナイザ（ku-nlp/gpt2-small-japanese-char 由来）は
convert_hf_to_gguf.py の pre-tokenizer の表に載っておらず、そのままでは
`BPE pre-tokenizer was not recognized` で止まる。中身は GPT-2 と同じ ByteLevel（正規表現あり）なので、
種別を `gpt-2` に固定して変換する。理由と確認方法は docs/INFER.md「pre-tokenizer」。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from eval.prompts import build_prompt

from .llama import ENV_DIR, LlamaServer, find_binary

PRE_TOKENIZER = "gpt-2"

# convert_hf_to_gguf.py を、pre-tokenizer の判定だけ差し替えて動かす。
# 判定は TextModel.get_vocab_base_pre（2025年の分割後は conversion/base.py、それ以前は本体。さらに古い版は Model）。
# あわせて、GPT-2 の変換は config.json の n_ctx を必須とするが、transformers 5 の GPT2Config は
# n_ctx を保存しない（n_positions だけ）。微調整後に保存し直したモデルで止まるので n_positions で補う
_BOOTSTRAP = """
import sys
llama_dir, pre = sys.argv[1], sys.argv[2]
sys.path.insert(0, llama_dir)
sys.argv = [llama_dir + "/convert_hf_to_gguf.py", *sys.argv[3:]]
import convert_hf_to_gguf as c
try:
    import conversion.base as base
except ImportError:
    base = c
cls = getattr(base, "TextModel", None) or getattr(base, "Model")
cls.get_vocab_base_pre = lambda self, tokenizer: pre
init = cls.__init__
def patched_init(self, *args, **kwargs):
    init(self, *args, **kwargs)
    if "n_positions" in self.hparams:
        self.hparams.setdefault("n_ctx", self.hparams["n_positions"])
cls.__init__ = patched_init
c.main()
"""

QUANT_TYPES = {"f16": "F16", "q8_0": "Q8_0", "q5_k_m": "Q5_K_M"}

# GGUF の特殊トークンの欄 → transformers のトークナイザの属性
_SPECIAL_KEYS = {"tokenizer.ggml.bos_token_id": "bos_token_id", "tokenizer.ggml.eos_token_id": "eos_token_id",
                 "tokenizer.ggml.padding_token_id": "pad_token_id", "tokenizer.ggml.unknown_token_id": "unk_token_id"}


def fix_special_tokens(gguf_path: Path, hf_model_dir: Path) -> dict[str, tuple[int, int]]:
    """GGUF の BOS/EOS/PAD/UNK の ID を、HF のトークナイザの値に書き換える（その場で。欄の大きさは変わらない）。

    convert_hf_to_gguf.py はこの ID を config.json から取る。zenz-v3.1 の config.json は bos=1・eos=2 だが、
    トークナイザの実際は [PAD]=1・<s>=2・</s>=3 で、そのままだと llama-server が <s> を EOS とみなす
    （docs/LLAMA_PARITY.md）。戻り値は書き換えた欄 → (前, 後)。
    """
    from gguf import GGUFReader
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(hf_model_dir)
    reader = GGUFReader(gguf_path, "r+")
    changed = {}
    for key, attr in _SPECIAL_KEYS.items():
        want = getattr(tok, attr, None)
        field = reader.get_field(key)
        if want is None or field is None:
            continue
        cur = int(field.parts[field.data[0]][0])
        if cur != want:
            field.parts[field.data[0]][0] = want
            changed[key] = (cur, want)
    reader.data.flush()
    return changed


def check_tokenizer(model_dir: Path) -> str:
    """pre-tokenizer を gpt-2 にしてよいトークナイザかを確かめ、判定の根拠を返す。だめなら ValueError。"""
    tj = model_dir / "tokenizer.json"
    if tj.is_file():
        t = json.loads(tj.read_text(encoding="utf-8"))
        # ku-nlp/gpt2-small-japanese-char の tokenizer.json は古い形式で model.type を持たない
        model_type = t.get("model", {}).get("type") or ("BPE" if "merges" in t.get("model", {}) else None)
        pre = t.get("pre_tokenizer") or {}
        if model_type != "BPE":
            raise ValueError(f"BPE ではない（model.type={model_type}）")
        if t.get("normalizer"):
            raise ValueError(f"normalizer がある（{t['normalizer'].get('type')}）。gpt-2 の pre-tokenizer では再現できない")
        if pre.get("type") != "ByteLevel" or pre.get("use_regex", True) is not True or pre.get("add_prefix_space"):
            raise ValueError(f"pre_tokenizer が GPT-2 と同じ ByteLevel（正規表現あり・先頭空白なし）ではない: {pre}")
        return "tokenizer.json: BPE + ByteLevel(use_regex, add_prefix_space=False), normalizer なし"
    cfg = model_dir / "tokenizer_config.json"
    cls = json.loads(cfg.read_text(encoding="utf-8")).get("tokenizer_class") if cfg.is_file() else None
    if cls in ("GPT2Tokenizer", "GPT2TokenizerFast") and (model_dir / "merges.txt").is_file():
        return f"{cls}（vocab.json + merges.txt）"
    raise ValueError(f"トークナイザの種類を判定できない: {model_dir}")


def convert(model_dir: Path, outfile: Path, outtype: str, llama_cpp: Path) -> None:
    cmd = [sys.executable, "-c", _BOOTSTRAP, str(llama_cpp), PRE_TOKENIZER,
           str(model_dir), "--outfile", str(outfile), "--outtype", outtype]
    subprocess.run(cmd, check=True)


def quantize(src: Path, dst: Path, qtype: str, bin_dir: Path | None, llama_cpp: Path) -> None:
    subprocess.run([str(find_binary("llama-quantize", bin_dir, llama_cpp)), str(src), str(dst), qtype], check=True)


def sample_texts() -> list[str]:
    """トークン化の一致を確かめる文。プロンプトそのものと、空白・改行・記号・英数字などの崩れやすい文字。"""
    from eval.converters import ROOT
    texts = [
        build_prompt("zenz-v3-romaji", "githubnipushshiteoite", ""),
        build_prompt("zenz-v3-romaji", "ashitanokaigi,nanjikaradakke?", "今日は雨だった。"),
        build_prompt("zenz-v3-romaji", "konnnichiha", "Hello, world! I'm here."),
        "今日はいい天気ですね。明日も晴れるといいな！",
        "pull requestを作ってくれ。 It's 3.14 or 1,000円？",
        "改行\nを含む\r\n文と\tタブと　全角空白",
        "記号 !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ と全角！＂＃＄％＆＇（）＊＋，－．／",
        "絵文字😀👍🏻と𠮷野家とｶﾀｶﾅとＡＢＣ１２３",
        "'s 't 're 've 'm 'll 'd I've you'll",
        "   先頭と末尾の空白   ",
        "",
    ]
    us = ROOT / "eval" / "sets" / "user_style.jsonl"
    if us.is_file():
        for line in us.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ex = json.loads(line)
                texts.append(build_prompt("zenz-v3-romaji", ex["romaji"], ex.get("left_context", "")))
                texts.append(ex["output"])
    return texts


def compare_tokenization(gguf: Path, hf_model_dir: Path, texts: list[str] | None = None,
                         bin_dir: Path | None = None) -> list[tuple[str, list[int], list[int]]]:
    """HF（add_special_tokens=False。評価ハーネスと同じ）と llama-server のトークン列を比べ、食い違いを返す。"""
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(hf_model_dir)
    texts = sample_texts() if texts is None else texts
    bad = []
    with LlamaServer(gguf, bin_dir=bin_dir) as server:
        for text in texts:
            hf_ids = tok(text, add_special_tokens=False).input_ids
            ll_ids = server.tokenize(text)
            if hf_ids != ll_ids:
                bad.append((text, hf_ids, ll_ids))
    return bad


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m infer.to_gguf", description=__doc__.split("\n\n")[0])
    p.add_argument("model", type=Path, help="HF 形式のモデルのディレクトリ")
    p.add_argument("--llama-cpp", type=Path, default=os.environ.get(ENV_DIR),
                   help=f"llama.cpp のソースのチェックアウト（convert_hf_to_gguf.py がある所。既定は ${ENV_DIR}）")
    p.add_argument("--llama-bin", type=Path, help="llama-quantize・llama-server のあるディレクトリ（既定は $LLAMA_CPP_BIN、"
                                                   "チェックアウトの build/bin、PATH）")
    p.add_argument("--outdir", type=Path, help="出力先（既定はモデルと同じ親ディレクトリ）")
    p.add_argument("--types", default="f16,q8_0,q5_k_m", help=f"作る形式（カンマ区切り。{', '.join(QUANT_TYPES)}）")
    p.add_argument("--check", action="store_true", help="作った GGUF で HF とトークン列が一致するか確かめる")
    p.add_argument("--fix-special", type=Path, nargs="+", metavar="GGUF",
                   help="変換はせず、作り済みの GGUF の BOS/EOS/PAD/UNK の ID を model のトークナイザの値に直すだけ")
    args = p.parse_args(argv)

    if args.fix_special:
        for path in args.fix_special:
            changed = fix_special_tokens(path, args.model)
            print(f"{path.name}: " + (", ".join(f"{k} {a}→{b}" for k, (a, b) in changed.items()) or "変更なし"),
                  file=sys.stderr)
        return 0

    if not args.llama_cpp or not (args.llama_cpp / "convert_hf_to_gguf.py").is_file():
        p.error(f"--llama-cpp に convert_hf_to_gguf.py のある llama.cpp のチェックアウトを指す（または ${ENV_DIR}）")
    types = [t.strip().lower() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in types if t not in QUANT_TYPES]
    if unknown:
        p.error(f"未知の形式: {unknown}")
    print(f"トークナイザ: {check_tokenizer(args.model)} → pre-tokenizer は {PRE_TOKENIZER}", file=sys.stderr)

    outdir = args.outdir or args.model.resolve().parent
    outdir.mkdir(parents=True, exist_ok=True)
    out = {t: outdir / f"{args.model.resolve().name}-{QUANT_TYPES[t]}.gguf" for t in types}

    # q8_0 は convert_hf_to_gguf.py が元の精度から直接作る。k-quant は llama-quantize で f16 から作る
    f16 = outdir / f"{args.model.resolve().name}-F16.gguf"
    if "f16" in types or "q5_k_m" in types:
        convert(args.model, f16, "f16", args.llama_cpp)
    if "q8_0" in types:
        convert(args.model, out["q8_0"], "q8_0", args.llama_cpp)
    if "q5_k_m" in types:
        quantize(f16, out["q5_k_m"], "Q5_K_M", args.llama_bin, args.llama_cpp)
    if "f16" not in types and f16.exists():
        f16.unlink()
    for path in out.values():
        changed = fix_special_tokens(path, args.model)
        if changed:
            print(f"特殊トークンの ID を直した {path.name}: "
                  + ", ".join(f"{k} {a}→{b}" for k, (a, b) in changed.items()), file=sys.stderr)
        print(f"作成: {path}（{path.stat().st_size / 1e6:.1f} MB）", file=sys.stderr)

    if args.check:
        bin_dir = args.llama_bin or find_binary("llama-server", None, args.llama_cpp).parent
        failed = False
        for t, path in out.items():
            bad = compare_tokenization(path, args.model, bin_dir=bin_dir)
            print(f"トークン列の照合 {path.name}: {'一致' if not bad else f'{len(bad)} 件食い違い'}", file=sys.stderr)
            for text, hf_ids, ll_ids in bad[:5]:
                print(f"  {text!r}\n    HF   {hf_ids}\n    GGUF {ll_ids}", file=sys.stderr)
            failed |= bool(bad)
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
