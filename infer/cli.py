"""ローマ字 → 日本語の CLI。

  python -m infer "githubnipushshiteoite" [--context "..."] [--backend hf|llama] [--model PATH]
  python -m infer --model models/zenz-romaji-v1-Q5_K_M.gguf     # 標準入力から1行ずつ（対話モード）

対話モードの1行は `ローマ字` か `左文脈<TAB>ローマ字`。
"""

import argparse
import sys
import time
from pathlib import Path

from eval.converters import ROOT

DEFAULT_HF_MODEL = ROOT / "models" / "zenz-romaji-v1"
DEFAULT_GGUF = ROOT / "models" / "zenz-romaji-v1-Q5_K_M.gguf"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m infer", description="ローマ字の打鍵列を日本語に変換する")
    p.add_argument("romaji", nargs="?", help="変換するローマ字。省くと標準入力から1行ずつ読む")
    p.add_argument("--context", default="", help="左文脈（直前の確定済みの文）")
    p.add_argument("--backend", choices=["hf", "llama"],
                   help="hf: transformers / llama: llama.cpp（llama-server）。省くと --model の拡張子 .gguf で決める")
    p.add_argument("--model", help=f"HF 形式のディレクトリか GGUF。既定は hf: {DEFAULT_HF_MODEL.relative_to(ROOT)}、"
                                   f"llama: {DEFAULT_GGUF.relative_to(ROOT)}")
    p.add_argument("--template", default="zenz-v3-romaji", help="プロンプト雛形（eval/prompts.py）")
    p.add_argument("--device", default="cpu", help="hf のみ: cpu / cuda")
    p.add_argument("--llama-bin", help="llama: llama-server のあるディレクトリ（既定は $LLAMA_CPP_BIN、PATH）")
    p.add_argument("--server-url", help="llama: 起動済みの llama-server に繋ぐ（例 http://127.0.0.1:8080）。--model は不要")
    p.add_argument("--threads", type=int, help="llama: スレッド数（-t）")
    p.add_argument("--ngl", type=int, help="llama: GPU に載せる層数（-ngl）")
    p.add_argument("-v", "--verbose", action="store_true", help="所要時間とトークン数を標準エラーに出す")
    args = p.parse_args(argv)
    if args.backend is None:
        args.backend = "llama" if (args.server_url or (args.model or "").lower().endswith(".gguf")) else "hf"
    if args.model is None and not (args.backend == "llama" and args.server_url):
        args.model = str(DEFAULT_GGUF if args.backend == "llama" else DEFAULT_HF_MODEL)
    return args


def make_backend(args: argparse.Namespace):
    if args.backend == "hf":
        from eval.converters import HFCausalLM
        if not Path(args.model).exists():
            raise SystemExit(f"モデルがない: {args.model}")
        return HFCausalLM(args.model, args.template, args.device)
    from .llama import LlamaCppLM
    return LlamaCppLM(args.model, args.template, url=args.server_url, bin_dir=args.llama_bin,
                      threads=args.threads, n_gpu_layers=args.ngl)


def _convert(conv, romaji: str, context: str, verbose: bool) -> str:
    t0 = time.perf_counter()
    r = conv.convert_detailed(romaji, context)
    if verbose:
        ms = (time.perf_counter() - t0) * 1000
        print(f"[{ms:.0f} ms, プロンプト {r.n_prompt_tokens} / 生成 {r.n_gen_tokens} トークン]", file=sys.stderr)
    return r.text


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Windows のコンソール既定（cp932 など）でも UTF-8 で読み書きする
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    conv = make_backend(args)
    try:
        if args.romaji is not None:
            print(_convert(conv, args.romaji, args.context, args.verbose))
            return 0
        interactive = sys.stdin.isatty()
        while True:
            if interactive:
                print("> ", end="", file=sys.stderr, flush=True)
            line = sys.stdin.readline()
            if not line:
                return 0
            line = line.rstrip("\r\n")
            context, _, romaji = line.rpartition("\t")
            if not romaji:
                print(flush=True)
                continue
            print(_convert(conv, romaji, context or args.context, args.verbose), flush=True)
    except KeyboardInterrupt:
        return 130
    finally:
        if hasattr(conv, "close"):
            conv.close()
