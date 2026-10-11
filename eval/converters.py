"""変換器の共通インターフェース: `convert(romaji, left_context="") -> str`。

HFCausalLM は Hugging Face の因果LM＋プロンプト雛形（prompts.py）。zenz-baseline は雛形 zenz-v3
（ローマ字を規則でカタカナにしてから元の zenz に渡す）にしたもの。romaji2ja も同じ枠で動く。
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .prompts import TEMPLATES, build_prompt

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Result:
    text: str
    n_prompt_tokens: int = 0
    n_gen_tokens: int = 0
    prompt: str = ""


class Converter(Protocol):
    name: str

    def convert(self, romaji: str, left_context: str = "") -> str: ...

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result: ...


class HFCausalLM:
    """因果LMを貪欲法で EOS まで回す。生成の上限は入力の長さに比例させる（文字単位モデルは出力 ≲ 入力）。"""

    def __init__(self, model: str, template: str, device: str = "cpu", name: str | None = None,
                 add_special_tokens: bool = False, dtype: str = "float32"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if template not in TEMPLATES:
            raise ValueError(f"未知の雛形: {template}")
        self.name = name or f"{Path(model).name}+{template}"
        self.template = template
        self.device = device
        self.add_special_tokens = add_special_tokens
        self._torch = torch
        self.tok = AutoTokenizer.from_pretrained(model)
        self.model = AutoModelForCausalLM.from_pretrained(model, dtype=getattr(torch, dtype)).to(device).eval()
        self.n_params = sum(p.numel() for p in self.model.parameters())

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result:
        torch, tok = self._torch, self.tok
        prompt = build_prompt(self.template, romaji, left_context)
        enc = tok(prompt, return_tensors="pt", add_special_tokens=self.add_special_tokens).to(self.device)
        n_prompt = enc.input_ids.shape[1]
        max_pos = getattr(self.model.config, "max_position_embeddings", 1024)
        cap = max(1, min(2 * len(romaji) + 16, max_pos - n_prompt))
        with torch.no_grad():
            out = self.model.generate(
                enc.input_ids, attention_mask=enc.attention_mask, max_new_tokens=cap, do_sample=False,
                eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id or tok.eos_token_id,
            )
        gen = out[0][n_prompt:]
        return Result(tok.decode(gen, skip_special_tokens=True), n_prompt, len(gen), prompt)

    def convert(self, romaji: str, left_context: str = "") -> str:
        return self.convert_detailed(romaji, left_context).text


def timed(conv: Converter, romaji: str, left_context: str) -> tuple[Result, float]:
    t0 = time.perf_counter()
    r = conv.convert_detailed(romaji, left_context)
    return r, (time.perf_counter() - t0) * 1000


def _local_or_hub(local: str, hub: str) -> str:
    p = ROOT / "models" / local
    return str(p) if p.exists() else hub


# 名前 → (モデル, 雛形)。モデルは --model で差し替えられる
PRESETS = {
    "romaji2ja": (lambda: _local_or_hub("romaji2ja", "limoXD/romaji2ja"), "romaji2ja-normalized"),
    "romaji2ja-raw": (lambda: _local_or_hub("romaji2ja", "limoXD/romaji2ja"), "romaji2ja"),
    "zenz-baseline": (lambda: "Miwa-Keita/zenz-v3.1-small", "zenz-v3"),
    "zenz-xsmall-baseline": (lambda: "Miwa-Keita/zenz-v3.1-xsmall", "zenz-v3"),
    "zenz-romaji": (None, "zenz-v3-romaji"),  # 微調整後のモデルを --model で指す
}


def make_converter(name: str, device: str = "cpu", model: str | None = None,
                   template: str | None = None) -> HFCausalLM:
    """name: PRESETS のキー、または `hf`（--model と --template を必ず指定）。"""
    if name == "hf":
        if not (model and template):
            raise ValueError("hf には --model と --template が要る")
        return HFCausalLM(model, template, device, name=f"hf:{Path(model).name}+{template}")
    if name not in PRESETS:
        raise ValueError(f"未知の変換器: {name}（{', '.join([*PRESETS, 'hf'])}）")
    default_model, default_template = PRESETS[name]
    model = model or (default_model() if default_model else None)
    if not model:
        raise ValueError(f"{name} には --model が要る")
    return HFCausalLM(model, template or default_template, device, name=name)
