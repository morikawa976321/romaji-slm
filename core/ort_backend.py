"""変換コアの生成と採点を ONNX Runtime（CPU）で行う。常駐入力は GPU の無い機械でこれを使う（docs/decisions.md D43）。

torch 版（core.constrained.ConstrainedZenz と core.select.fluency・ZenzScorer.logprobs）と同じ手順・同じ照合で、forward だけ
ONNX Runtime に替える。CPU の torch は 1 手の forward の固定費が支配的（D3）で、同じ計算を軽い実行系で回すと、照合も探索も
変えずに速くなる（全評価セット 2400 文で出力が torch 版と同じ、CPU で 0.72 倍の時間。docs/LLAMA_PARITY.md）。
GPT-2 を KV キャッシュを明示した形（_Step）に移して ONNX に書き出す（初回だけ torch が要る）。logits の差は HF と 3e-5 以内で、
書き出した後に HF と照らし、外れていれば使わない。
"""

import os
from pathlib import Path

import numpy as np

from .constrained import Decoded, LatticeMatcher, _char_bounds, bytes_to_unicode
from .kanakanji import Dictionary
from .pipeline import CorePipeline
from .select import scoring_text, zenz_prompt
from .types import Reading

ROOT = Path(__file__).resolve().parent.parent


def export_onnx(model: str, out: Path) -> None:
    import torch
    import torch.nn as nn
    from transformers import AutoModelForCausalLM

    hf = AutoModelForCausalLM.from_pretrained(model, dtype=torch.float32).eval()
    E, H = hf.config.n_embd, hf.config.n_head
    D, L = E // H, hf.config.n_layer

    class _Step(nn.Module):
        def __init__(self):
            super().__init__()
            self.t, self.lm = hf.transformer, hf.lm_head

        def forward(self, ids, pos, mask, pk, pv):
            # ids, pos: [B,S]、mask: [B, Tp+S]（1 = 見てよい）、pk, pv: [L,B,H,Tp,D]
            x = self.t.wte(ids) + self.t.wpe(pos)
            B, S = ids.shape
            Tp = pk.shape[3]
            causal = torch.ones(S, Tp + S, dtype=torch.bool).tril(Tp)
            allow = causal[None, None] & (mask[:, None, None, :] > 0)
            bias = torch.where(allow, 0.0, torch.finfo(torch.float32).min)
            nk, nv = [], []
            for i, blk in enumerate(self.t.h):
                q, k, v = blk.attn.c_attn(blk.ln_1(x)).split(E, dim=2)
                q = q.view(B, S, H, D).transpose(1, 2)
                k = torch.cat([pk[i], k.view(B, S, H, D).transpose(1, 2)], dim=2)
                v = torch.cat([pv[i], v.view(B, S, H, D).transpose(1, 2)], dim=2)
                nk.append(k)
                nv.append(v)
                a = torch.softmax((q @ k.transpose(-1, -2)) / (D ** 0.5) + bias, dim=-1) @ v
                x = x + blk.attn.c_proj(a.transpose(1, 2).reshape(B, S, E))
                x = x + blk.mlp(blk.ln_2(x))
            return self.lm(self.t.ln_f(x)), torch.stack(nk), torch.stack(nv)

    B, P = 2, 4
    args = (torch.zeros(B, 1, dtype=torch.long), torch.full((B, 1), P), torch.ones(B, P + 1, dtype=torch.long),
            torch.zeros(L, B, H, P, D), torch.zeros(L, B, H, P, D))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    # 書き終えてから名前を付け替える（途中で止まっても、壊れたファイルを次の起動で使わない）
    torch.onnx.export(_Step().eval(), args, str(tmp), input_names=["ids", "pos", "mask", "pk", "pv"],
                      output_names=["logits", "nk", "nv"],
                      dynamic_axes={"ids": {0: "B", 1: "S"}, "pos": {0: "B", 1: "S"}, "mask": {0: "B", 1: "T"},
                                    "pk": {1: "B", 3: "Tp"}, "pv": {1: "B", 3: "Tp"}, "logits": {0: "B", 1: "S"},
                                    "nk": {1: "B", 3: "T"}, "nv": {1: "B", 3: "T"}}, opset_version=17, dynamo=False)
    # 書き出した ONNX を HF と照らす（プロンプト 1 回と、KV キャッシュ付きの 1 手）。外れていれば使わない
    import onnxruntime as ort
    sess = ort.InferenceSession(str(tmp), providers=["CPUExecutionProvider"])
    g = torch.Generator().manual_seed(0)
    ids = torch.randint(5, hf.config.vocab_size, (2, 12), generator=g)
    with torch.no_grad():
        ref = hf(input_ids=ids, use_cache=True)
        ref2 = hf(input_ids=ids[:, :1], past_key_values=ref.past_key_values, use_cache=True)
    e = np.zeros((L, 2, H, 0, D), dtype=np.float32)
    lg, nk, nv = sess.run(None, {"ids": ids.numpy(), "pos": np.tile(np.arange(12), (2, 1)), "mask": np.ones((2, 12), np.int64),
                                 "pk": e, "pv": e})
    lg2 = sess.run(None, {"ids": ids[:, :1].numpy(), "pos": np.full((2, 1), 12), "mask": np.ones((2, 13), np.int64),
                          "pk": nk, "pv": nv})[0]
    diff = max(float(np.abs(lg - ref.logits.numpy()).max()), float(np.abs(lg2 - ref2.logits.numpy()).max()))
    del sess
    if diff > _EXPORT_TOL:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"書き出した ONNX の logits が HF と {diff:.2e} 違う（許すのは {_EXPORT_TOL}）")
    os.replace(tmp, out)


_EXPORT_TOL = 1e-3  # 実測は 3e-5 以内（fp32 の和の順序の差）


def _log_softmax(x: np.ndarray) -> np.ndarray:
    m = x.max(axis=-1, keepdims=True)
    return x - m - np.log(np.exp(x - m).sum(axis=-1, keepdims=True))


class OrtBackend:
    """CorePipeline に注入する生成と採点（decode_batch・fluency）。手順は ConstrainedZenz.decode_batch と core.select.fluency と同じ。"""

    _TOP = 64

    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", onnx_path: str | Path | None = None,
                 threads: int | None = None, max_tries: int = 2000):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        path = Path(onnx_path) if onnx_path else ROOT / "models" / "onnx" / f"{Path(model).name}.onnx"
        if not path.exists():
            export_onnx(model, path)
        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        self.tok = AutoTokenizer.from_pretrained(model)
        dec = {c: b for b, c in bytes_to_unicode().items()}
        self.tok_bytes = [bytes(dec[ch] for ch in t) if t is not None else b""
                          for t in self.tok.convert_ids_to_tokens(list(range(len(self.tok))))]
        self.eos = self.tok.eos_token_id
        self.pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.eos
        self.max_tries = max_tries
        shp = {i.name: i.shape for i in self.sess.get_inputs()}["pk"]
        self.L, self.H, self.D = shp[0], shp[2], shp[4]
        self.n_params = 0

    def _run(self, ids, pos, mask, pk, pv):
        return self.sess.run(None, {"ids": ids, "pos": pos, "mask": mask, "pk": pk, "pv": pv})

    def _empty(self, B: int) -> np.ndarray:
        return np.zeros((self.L, B, self.H, 0, self.D), dtype=np.float32)

    def _pick(self, logits: np.ndarray, m: LatticeMatcher, states):
        n_top = min(self._TOP, self.max_tries, logits.shape[-1])
        part = np.argpartition(-logits, n_top - 1)[:n_top]
        top = part[np.argsort(-logits[part], kind="stable")].tolist()
        t, ns = self._scan(top, m, states)
        if t is not None or self.max_tries <= n_top:
            return t, ns
        order = np.argsort(-logits, kind="stable")[n_top:self.max_tries].tolist()
        return self._scan(order, m, states)

    def _scan(self, order, m: LatticeMatcher, states):
        for t in order:
            if t == self.eos:
                if m.accepting(states):
                    return t, states
                continue
            b = self.tok_bytes[t]
            if not b:
                continue
            ns = m.advance(states, b)
            if ns:
                return t, ns
        return None, None

    def decode_batch(self, readings: list[Reading], dic: Dictionary, left_context: str = "",
                     max_new: int | None = None, punct: str = "auto", en_case: str = "zenz") -> list[Decoded]:
        if not readings:
            return []
        matchers = [LatticeMatcher(r, dic, punct, en_case) for r in readings]
        states = [m.start() for m in matchers]
        prompts = [self.tok(zenz_prompt(r.zenz_input(), left_context), add_special_tokens=False).input_ids
                   for r in readings]
        B, P = len(prompts), max(len(p) for p in prompts)
        ids = np.full((B, P), self.pad, dtype=np.int64)
        mask = np.zeros((B, P), dtype=np.int64)
        for i, p in enumerate(prompts):
            ids[i, P - len(p):] = p
            mask[i, P - len(p):] = 1
        pos = np.clip(mask.cumsum(-1) - 1, 0, None)
        limits = [max_new or 3 * len(r.zenz_input()) + 8 for r in readings]
        outs = [bytearray() for _ in readings]
        logp = [0.0] * B
        finished = [False] * B
        alive = [True] * B
        bounds: list[list[int]] = [[] for _ in readings]
        pk = pv = self._empty(B)
        step_ids, step_pos = ids, pos
        for step in range(max(limits)):
            logits, pk, pv = self._run(step_ids, step_pos, mask, pk, pv)
            last = logits[:, -1].astype(np.float32)
            nxt = [self.pad] * B
            for i in range(B):
                if not alive[i]:
                    continue
                if step >= limits[i]:
                    alive[i] = False
                    continue
                chosen, ns = self._pick(last[i], matchers[i], states[i])
                if chosen is None:
                    alive[i] = False
                    continue
                logp[i] += float(_log_softmax(last[i])[chosen])
                if chosen == self.eos:
                    finished[i] = True
                    alive[i] = False
                    continue
                base = len(outs[i])
                bounds[i].extend(base + k for k in matchers[i].advance_bounds(states[i], self.tok_bytes[chosen])[1])
                states[i] = ns
                outs[i] += self.tok_bytes[chosen]
                nxt[i] = chosen
            if not any(alive):
                break
            step_ids = np.array(nxt, dtype=np.int64)[:, None]
            mask = np.concatenate([mask, np.ones((B, 1), dtype=np.int64)], axis=1)
            step_pos = step_pos[:, -1:] + np.array([[int(a)] for a in alive], dtype=np.int64)
        return [Decoded(o.decode("utf-8", errors="replace"), lp, f, _char_bounds(bytes(o), bd))
                for o, lp, f, bd in zip(outs, logp, finished, bounds)]

    _BATCH = 32  # ZenzScorer と同じ（1 回に流す系列の数。候補ウィンドウの別の書き方は 2 × 件数の系列になる）

    def logprobs(self, prompts: list[str], continuations: list[str], eos: bool | list[bool] = True) -> list[float]:
        """core.select.ZenzScorer.logprobs と同じ（右詰めのパディング、続きのトークンの対数確率の和）。
        eos は系列ごとにも渡せる（候補ウィンドウ core.window が変換の確率と自然さを 1 回で測る）。"""
        eos = eos if isinstance(eos, list) else [eos] * len(prompts)
        seqs = []
        for p, c, e in zip(prompts, continuations, eos):
            p_ids = self.tok(p, add_special_tokens=False).input_ids
            c_ids = self.tok(c, add_special_tokens=False).input_ids + ([self.eos] if e else [])
            seqs.append((p_ids + c_ids, len(p_ids)))
        out = []
        for k in range(0, len(seqs), self._BATCH):
            chunk = seqs[k:k + self._BATCH]
            B, T = len(chunk), max(len(s) for s, _ in chunk)
            ids = np.full((B, T), self.pad, dtype=np.int64)
            mask = np.zeros((B, T), dtype=np.int64)
            for r, (s, _) in enumerate(chunk):
                ids[r, :len(s)] = s
                mask[r, :len(s)] = 1
            pos = np.broadcast_to(np.arange(T, dtype=np.int64), (B, T)).copy()
            logits = self._run(ids, pos, mask, self._empty(B), self._empty(B))[0]
            for r, (s, n_p) in enumerate(chunk):
                lp = _log_softmax(logits[r, n_p - 1:len(s) - 1].astype(np.float32))
                out.append(float(lp[np.arange(len(s) - n_p), s[n_p:]].sum()))
        return out

    def fluency(self, texts: list[str], left_context: str = "") -> list[float]:
        from eval.prompts import CONTEXT, INPUT
        return self.logprobs([CONTEXT + left_context] * len(texts), [scoring_text(t) + INPUT for t in texts], eos=False)


class OrtCorePipeline(CorePipeline):
    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", *, threads: int | None = None,
                 onnx_path: str | Path | None = None, name: str | None = None, **kwargs):
        super().__init__(name=name or "core-ort", backend=OrtBackend(model, onnx_path=onnx_path, threads=threads),
                         **kwargs)
