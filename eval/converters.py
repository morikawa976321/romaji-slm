"""変換器の共通インターフェース: `convert(romaji, left_context="") -> str`。

HFCausalLM は Hugging Face の因果LM＋プロンプト雛形（prompts.py）。zenz-baseline は雛形 zenz-v3
（ローマ字を規則でカタカナにしてから元の zenz に渡す）にしたもの。romaji2ja も同じ枠で動く。
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .prompts import CONTEXT, INPUT, OUTPUT, TEMPLATES, TWO_STAGE, build_prompt, split_two_stage

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Result:
    text: str
    n_prompt_tokens: int = 0
    n_gen_tokens: int = 0
    prompt: str = ""
    candidates: tuple[str, ...] = ()  # 候補を出す変換器だけ: 順位順の候補（oracle の計算に使う）
    edited: bool | None = None  # 打鍵を補正する変換器だけ: 1位が打鍵の補正を含むか（誤補正率の計算に使う）


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
        # 二段形式は読みも書くので、上限を読みの分（入力のローマ字より短い）だけ広げる
        per_char = 3 if self.template in TWO_STAGE else 2
        cap = max(1, min(per_char * len(romaji) + 16, max_pos - n_prompt))
        with torch.no_grad():
            out = self.model.generate(
                enc.input_ids, attention_mask=enc.attention_mask, max_new_tokens=cap, do_sample=False,
                eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id or tok.eos_token_id,
            )
        gen = out[0][n_prompt:]
        text = tok.decode(gen, skip_special_tokens=True)
        if self.template in TWO_STAGE:
            text = split_two_stage(text)[1]
        return Result(text, n_prompt, len(gen), prompt)

    def convert(self, romaji: str, left_context: str = "") -> str:
        return self.convert_detailed(romaji, left_context).text


class ReaderPlusZenz:
    """読みは二段形式で微調整したモデルに書かせ、変換は元の zenz に任せる。

    二段形式の微調整は、読みの段は覚えるが変換の段の知識を削る（v2: 正解の読みを与えても AJIMEE で 87%→72.5%）。
    変換の段を元の重みのまま使うための組み合わせ。生成するのは読みまで（OUTPUT が出た時点で止める）。
    """

    def __init__(self, reader: str, converter: str = "Miwa-Keita/zenz-v3.1-small", device: str = "cpu",
                 name: str | None = None):
        self.reader = HFCausalLM(reader, "zenz-v3-romaji-reading", device)
        self.conv = HFCausalLM(converter, "zenz-v3", device)
        self.name = name or f"{Path(reader).name}+{Path(converter).name}"
        self.n_params = self.reader.n_params + self.conv.n_params
        out_ids = self.reader.tok(OUTPUT, add_special_tokens=False).input_ids
        self._stop = out_ids

    def _read(self, romaji: str, left_context: str) -> tuple[str, int, int]:
        r, tok, torch = self.reader, self.reader.tok, self.reader._torch
        enc = tok(build_prompt(r.template, romaji, left_context), return_tensors="pt",
                  add_special_tokens=False).to(r.device)
        n = len(self._stop)

        stop = self._stop

        class _StopAtOutput:
            def __call__(self, input_ids, scores, **kw):
                return input_ids[0, -n:].tolist() == stop

        from transformers import StoppingCriteriaList
        with torch.no_grad():
            out = r.model.generate(enc.input_ids, attention_mask=enc.attention_mask,
                                   max_new_tokens=2 * len(romaji) + 16, do_sample=False,
                                   eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id or tok.eos_token_id,
                                   stopping_criteria=StoppingCriteriaList([_StopAtOutput()]))
        gen = out[0][enc.input_ids.shape[1]:]
        reading = split_two_stage(tok.decode(gen, skip_special_tokens=True) + OUTPUT)[0]
        return reading, enc.input_ids.shape[1], len(gen)

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result:
        reading, n_p, n_g = self._read(romaji, left_context)
        c, tok, torch = self.conv, self.conv.tok, self.conv._torch
        head = CONTEXT + left_context if left_context else ""
        prompt = f"{head}{INPUT}{reading}{OUTPUT}"
        enc = tok(prompt, return_tensors="pt", add_special_tokens=False).to(c.device)
        with torch.no_grad():
            out = c.model.generate(enc.input_ids, attention_mask=enc.attention_mask,
                                   max_new_tokens=2 * len(reading) + 16, do_sample=False,
                                   eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id or tok.eos_token_id)
        gen = out[0][enc.input_ids.shape[1]:]
        return Result(tok.decode(gen, skip_special_tokens=True), n_p + enc.input_ids.shape[1], n_g + len(gen),
                      prompt)

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
    "zenz-romaji-reading": (None, "zenz-v3-romaji-reading"),  # 二段形式で微調整したモデル
}


def make_converter(name: str, device: str = "cpu", model: str | None = None,
                   template: str | None = None) -> HFCausalLM:
    """name: PRESETS のキー、または `hf`（--model と --template を必ず指定）。"""
    if name == "hf":
        if not (model and template):
            raise ValueError("hf には --model と --template が要る")
        return HFCausalLM(model, template, device, name=f"hf:{Path(model).name}+{template}")
    if name == "core" or name.startswith(("core:", "core-llama")):
        # core / core:k=3 / core:k=3,w=1.0,f=1,b=0（b=0 は k 件を1件ずつ生成。速度の比較用）
        opts = dict(kv.split("=") for kv in name.partition(":")[2].split(",") if kv)
        # p=auto|ja|ascii|insert（句読点）、e=zenz|typed|registered|auto（英字の表記）、tc/te/tq=句読点 B の閾値（読点・句点・疑問符）
        kw = dict(k=int(opts.get("k", 5)), w_read=float(opts.get("w", 4.0)), w_flu=float(opts.get("f", 1.0)),
                  punct=opts.get("p", "auto"),  # 評価の既定は auto のまま（過去の表と比べるため。本人の既定は insert）
                  en_case=opts.get("e", "typed"), tau_comma=float(opts.get("tc", -0.5)),
                  tau_end=float(opts.get("te", -6.0)), tau_q=float(opts.get("tq", -5.0)), name=name,
                  preselect=opts.get("ps", "1") == "1",  # ps=0 で読み段の上位をそのまま使う（D18 以前の振る舞い）
                  punct_end=opts.get("pe", "0") == "1",  # pe=1 で句読点 B が文末の句点も補う（D31 より前の既定）
                  user_dict=opts.get("ud") or None)  # ud=<パス> で本人のユーザー辞書を読む（docs/REVIEW.md の検証）
        if name.startswith("core-llama"):
            # core-llama:np=5,t=8,...（--model は GGUF。np=llama-server のスロット数、t=スレッド数、
            # url=起動済みの llama-server に繋ぐ）
            from core.llama_backend import LlamaCorePipeline
            if not (model or "url" in opts):
                raise ValueError("core-llama には --model（GGUF）か url= が要る")
            # 変種（docs/LLAMA_PARITY.md）: de=stop（行き止まりで未完にする）、fl=tok（自然さを正規の分割で強制）、
            # rf=fast（取り直しをまとめる）、ka=1（HTTP の接続を使い回す）、ngl=<層数>（GPU に載せる層）、
            # eos=3（GGUF の BOS/EOS の ID を起動時に上書きする。zenz の config.json は bos=1・eos=2 と書いていて、
            # GGUF にはそれがそのまま入る。トークナイザの本当の値は <s>=2・</s>=3）
            extra = (["--override-kv", "tokenizer.ggml.bos_token_id=int:2,tokenizer.ggml.eos_token_id=int:3"]
                     if opts.get("eos") == "3" else None)
            return LlamaCorePipeline(model, url=opts.get("url"), n_slots=int(opts.get("np", 5)), extra_args=extra,
                                     threads=int(opts["t"]) if "t" in opts else None,
                                     n_gpu_layers=int(opts["ngl"]) if "ngl" in opts else None,
                                     dead_ends=opts.get("de", "prune"), fluency_mode=opts.get("fl", "gen"),
                                     refill=opts.get("rf", "full"), keep_alive=opts.get("ka", "0") == "1", **kw)
        from core.pipeline import CorePipeline
        # mt=<数>（制約を満たすトークンを探す上位の数。0 は語彙全体）、dp=1（行き止まりを刈る。llama 版の文法と同じ制約）
        return CorePipeline(model or "Miwa-Keita/zenz-v3.1-small", device, batched=opts.get("b", "1") != "0",
                            max_tries=int(opts.get("mt", 2000)), prune=opts.get("dp", "0") == "1", **kw)
    if name == "reader+zenz":
        if not model:
            raise ValueError("reader+zenz には --model（読みを書く二段形式のモデル）が要る")
        return ReaderPlusZenz(model, device=device, name=f"reader+zenz:{Path(model).name}")
    if name not in PRESETS:
        raise ValueError(f"未知の変換器: {name}（{', '.join([*PRESETS, 'hf'])}）")
    default_model, default_template = PRESETS[name]
    model = model or (default_model() if default_model else None)
    if not model:
        raise ValueError(f"{name} には --model が要る")
    return HFCausalLM(model, template or default_template, device, name=name)
