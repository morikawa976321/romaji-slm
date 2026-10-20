"""変換コアの生成と採点を ONNX Runtime（CPU）で行う。常駐入力は GPU の無い機械でこれを使う（docs/decisions.md D43）。

torch 版（core.constrained.ConstrainedZenz と core.select.fluency・ZenzScorer.logprobs）と同じ手順・同じ照合で、forward だけ
ONNX Runtime に替える。CPU の torch は 1 手の forward の固定費が支配的（D3）で、同じ計算を軽い実行系で回すと、照合も探索も
変えずに速くなる（全評価セット 2400 文で出力が torch 版と同じ、CPU で 0.72 倍の時間。docs/LLAMA_PARITY.md）。
GPT-2 を KV キャッシュを明示した形（_Step）に移して ONNX に書き出す（初回だけ torch が要る）。logits の差は HF と 3e-5 以内で、
書き出した後に HF と照らし、外れていれば使わない。

速くする工夫（docs/SPEEDUP.md）。どれも計算の中身は変えない:
- 行どうし・要求どうしで共通の前置き（左文脈など）の KV は 1 回だけ計算して取っておき、各行に写す（_prefix・_plan）。
  常駐入力は左文脈を最大 60 字送り、生成・自然さ・別の書き方の全行の頭に付くので、これを毎回計算し直すと 1 要求が約 2 倍になる。
- KV は層ごとの入出力にする。全層を 1 つに束ねると、1 手ごとに KV を 3 回写す（層への切り出し・連結・束ね直し）。
- 書き出した後に onnxruntime.transformers で FastGelu・SkipLayerNorm を融合する。
"""

import os
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import cancel
from .constrained import Decoded, LatticeMatcher, _char_bounds, bytes_to_unicode
from .kanakanji import Dictionary
from .pipeline import CorePipeline
from .select import scoring_text, zenz_prompt
from .types import Reading

ROOT = Path(__file__).resolve().parent.parent
# 書き出しの形の版。入出力を変えたら上げる（ファイル名が変わり、次の起動で書き出し直す）。v1 は版の付かない名前で、KV を全層束ねていた
_FORMAT = "v2"


def default_path(model: str) -> Path:
    return ROOT / "models" / "onnx" / f"{Path(model).name}-{_FORMAT}.onnx"


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

        def forward(self, ids, pos, mask, *past):
            # ids, pos: [B,S]、mask: [B, Tp+S]（1 = 見てよい）、past: 層ごとの k が L 個、続いて v が L 個（各 [B,H,Tp,D]）
            pk, pv = past[:L], past[L:]
            x = self.t.wte(ids) + self.t.wpe(pos)
            B, S = ids.shape
            Tp = pk[0].shape[2]
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
            return (self.lm(self.t.ln_f(x)), *nk, *nv)

    pk_n, pv_n = [f"pk{i}" for i in range(L)], [f"pv{i}" for i in range(L)]
    nk_n, nv_n = [f"nk{i}" for i in range(L)], [f"nv{i}" for i in range(L)]
    B, P = 2, 4
    args = (torch.zeros(B, 1, dtype=torch.long), torch.full((B, 1), P), torch.ones(B, P + 1, dtype=torch.long),
            *[torch.zeros(B, H, P, D) for _ in range(2 * L)])
    axes = {"ids": {0: "B", 1: "S"}, "pos": {0: "B", 1: "S"}, "mask": {0: "B", 1: "T"}, "logits": {0: "B", 1: "S"},
            **{n: {0: "B", 2: "Tp"} for n in pk_n + pv_n}, **{n: {0: "B", 2: "T"} for n in nk_n + nv_n}}
    out.parent.mkdir(parents=True, exist_ok=True)
    raw, tmp = out.with_name(out.name + ".raw.tmp"), out.with_name(out.name + ".tmp")
    # 書き終えてから名前を付け替える（途中で止まっても、壊れたファイルを次の起動で使わない）
    torch.onnx.export(_Step().eval(), args, str(raw), input_names=["ids", "pos", "mask", *pk_n, *pv_n],
                      output_names=["logits", *nk_n, *nv_n], dynamic_axes=axes, opset_version=17, dynamo=False)
    try:
        # FastGelu 12・SkipLayerNorm 25 に融合される（Attention は形が合わず融合されない）。1 手 2〜7% 速い（SPEEDUP.md §2）
        from onnxruntime.transformers.optimizer import optimize_model
        optimize_model(str(raw), model_type="gpt2", num_heads=H, hidden_size=E, opt_level=1,
                       use_gpu=False).save_model_to_file(str(tmp))
        raw.unlink()
    except Exception:  # 融合できなくても、融合しない書き出しで動く（下の照合は同じく通す）
        os.replace(raw, tmp)
    # 書き出した ONNX を HF と照らす（プロンプト 1 回と、KV キャッシュ付きの 1 手）。外れていれば使わない
    import onnxruntime as ort
    sess = ort.InferenceSession(str(tmp), providers=["CPUExecutionProvider"])
    g = torch.Generator().manual_seed(0)
    ids = torch.randint(5, hf.config.vocab_size, (2, 12), generator=g)
    with torch.no_grad():
        ref = hf(input_ids=ids, use_cache=True)
        ref2 = hf(input_ids=ids[:, :1], past_key_values=ref.past_key_values, use_cache=True)
    e = np.zeros((2, H, 0, D), dtype=np.float32)
    o = sess.run(None, {"ids": ids.numpy(), "pos": np.tile(np.arange(12), (2, 1)), "mask": np.ones((2, 12), np.int64),
                        **{n: e for n in pk_n + pv_n}})
    lg2 = sess.run(None, {"ids": ids[:, :1].numpy(), "pos": np.full((2, 1), 12), "mask": np.ones((2, 13), np.int64),
                          **dict(zip(pk_n + pv_n, o[1:]))})[0]
    diff = max(float(np.abs(o[0] - ref.logits.numpy()).max()), float(np.abs(lg2 - ref2.logits.numpy()).max()))
    del sess
    if diff > _EXPORT_TOL:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"書き出した ONNX の logits が HF と {diff:.2e} 違う（許すのは {_EXPORT_TOL}）")
    os.replace(tmp, out)


_EXPORT_TOL = 1e-3  # 実測は 4e-5 以内（fp32 の和の順序の差。融合しても同じ桁）


def _log_softmax(x: np.ndarray) -> np.ndarray:
    m = x.max(axis=-1, keepdims=True)
    return x - m - np.log(np.exp(x - m).sum(axis=-1, keepdims=True))


def _lcp(a, b) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class OrtBackend:
    """CorePipeline に注入する生成と採点（decode_batch・fluency・logprobs）。手順は ConstrainedZenz.decode_batch と
    core.select.fluency・ZenzScorer.logprobs と同じ。"""

    _TOP = 64
    # 取っておく前置きの KV の数。1 つで 1 トークンあたり約 74 KB（12 層 × 768 × fp32 × k・v）、左文脈 60 字で 1 つ約 7 MB。
    # 1 要求で使う前置きは 4 種まで（生成の共通部・自然さ・別の書き方の 2 つ）で、左文脈は要求をまたいで同じ
    _CACHE = 8

    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", onnx_path: str | Path | None = None,
                 threads: int | None = None, max_tries: int = 2000, share: bool = True):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        path = Path(onnx_path) if onnx_path else default_path(model)
        if not path.exists():
            export_onnx(model, path)
            if not onnx_path:  # 旧形式（v1）の書き出しは使わないので消す（380 MB）
                (path.parent / f"{Path(model).name}.onnx").unlink(missing_ok=True)
        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        self._ort = ort
        self.tok = AutoTokenizer.from_pretrained(model)
        dec = {c: b for b, c in bytes_to_unicode().items()}
        self.tok_bytes = [bytes(dec[ch] for ch in t) if t is not None else b""
                          for t in self.tok.convert_ids_to_tokens(list(range(len(self.tok))))]
        self.eos = self.tok.eos_token_id
        self.pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.eos
        self.max_tries = max_tries
        inputs = {i.name: i.shape for i in self.sess.get_inputs()}
        self.L = sum(1 for n in inputs if n.startswith("pk"))
        self.H, self.D = inputs["pk0"][1], inputs["pk0"][3]
        self._pk, self._pv = [f"pk{i}" for i in range(self.L)], [f"pv{i}" for i in range(self.L)]
        self.share = share
        self.cache: OrderedDict = OrderedDict()
        self.n_params = 0

    def _run(self, ids, pos, mask, pk, pv):
        # 常駐入力の古い先読みは、forward の前と、走っている forward の途中（RunOptions.terminate）でやめる（core.cancel）
        cancel.check()
        feed = {"ids": ids, "pos": pos, "mask": mask, **dict(zip(self._pk, pk)), **dict(zip(self._pv, pv))}
        stale = cancel.stopper()
        if stale is None:
            out = self.sess.run(None, feed)
        else:
            ro = self._ort.RunOptions()
            with cancel.running(stale, lambda: setattr(ro, "terminate", True)):
                try:
                    out = self.sess.run(None, feed, ro)
                except Exception:
                    if ro.terminate:
                        raise cancel.Cancelled from None
                    raise
        return out[0], out[1:1 + self.L], out[1 + self.L:]

    def _empty(self, B: int) -> list[np.ndarray]:
        return [np.zeros((B, self.H, 0, self.D), dtype=np.float32)] * self.L

    # --- 共通の前置き ---

    def _prefix(self, toks: tuple):
        """前置き toks の KV（B=1）。取ってある最長の前置きから伸ばして計算し、取っておく。"""
        if toks in self.cache:
            self.cache.move_to_end(toks)
            return self.cache[toks]
        best = max((k for k in self.cache if len(k) < len(toks) and toks[:len(k)] == k), key=len, default=())
        if best:
            self.cache.move_to_end(best)
            pk, pv = self.cache[best]
        else:
            pk = pv = self._empty(1)
        T = len(toks)
        _, pk, pv = self._run(np.array([toks[len(best):]], dtype=np.int64),
                              np.arange(len(best), T, dtype=np.int64)[None], np.ones((1, T), dtype=np.int64), pk, pv)
        self.cache[toks] = (pk, pv)
        while len(self.cache) > self._CACHE:
            self.cache.popitem(last=False)
        return pk, pv

    def _plan(self, seqs: list[tuple[list[int], int]]) -> list[int]:
        """行ごとの前置きの長さ（(トークン列, プロンプトの長さ) ごと）。同じプロンプトの行が 2 つ以上あればその行どうし、
        1 つだけなら全行の共通部。どれもプロンプトの最後の 1 トークンは残す（続きの最初の確率を後半の logits から取る）。"""
        if not self.share or len(seqs) < 2:
            return [0] * len(seqs)
        heads = [tuple(s[:n_p - 1]) for s, n_p in seqs]
        common = min(_lcp(heads[0], h) for h in heads)
        groups: dict = {}
        for i, (s, n_p) in enumerate(seqs):
            groups.setdefault(tuple(s[:n_p]), []).append(i)
        out = [common] * len(seqs)
        for idx in groups.values():
            if len(idx) >= 2:
                for i in idx:
                    out[i] = len(heads[i])
        return [n if n >= 2 else 0 for n in out]  # 1 トークンの前置きは別に計算するほうが高い

    def _assemble(self, rows: list[list[int]], plen: list[int]):
        """各行の前置きの KV を左詰めで並べた past（前置きの短い行の残りは mask 0）と、その mask。"""
        B, Tp = len(rows), max(plen)
        pk = [np.zeros((B, self.H, Tp, self.D), dtype=np.float32) for _ in range(self.L)]
        pv = [np.zeros((B, self.H, Tp, self.D), dtype=np.float32) for _ in range(self.L)]
        pmask = np.zeros((B, Tp), dtype=np.int64)
        for r, (t, n) in enumerate(zip(rows, plen)):
            if n:
                k, v = self._prefix(tuple(t[:n]))
                for i in range(self.L):
                    pk[i][r, :, :n] = k[i][0]
                    pv[i][r, :, :n] = v[i][0]
                pmask[r, :n] = 1
        return pk, pv, pmask

    # --- 生成 ---

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
        B = len(prompts)
        plen = self._plan([(p, len(p)) for p in prompts])
        pk, pv, pmask = self._assemble(prompts, plen)
        # 前置きの後ろの残りは左に詰め（最後の位置を全行で揃える）、位置は行の前置きの長さから続ける
        S = max(len(p) - n for p, n in zip(prompts, plen))
        ids = np.full((B, S), self.pad, dtype=np.int64)
        smask = np.zeros((B, S), dtype=np.int64)
        for i, (p, n) in enumerate(zip(prompts, plen)):
            ids[i, S - (len(p) - n):] = p[n:]
            smask[i, S - (len(p) - n):] = 1
        pos = np.array(plen, dtype=np.int64)[:, None] + np.clip(smask.cumsum(-1) - 1, 0, None)
        mask = np.concatenate([pmask, smask], axis=1)
        limits = [max_new or 3 * len(r.zenz_input()) + 8 for r in readings]
        outs = [bytearray() for _ in readings]
        logp = [0.0] * B
        finished = [False] * B
        alive = [True] * B
        bounds: list[list[int]] = [[] for _ in readings]
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

    # --- 採点 ---

    _BATCH = 32  # ZenzScorer と同じ（1 回に流す系列の数。候補ウィンドウの別の書き方は 2 × 件数の系列になる）

    def logprobs(self, prompts: list[str], continuations: list[str], eos: bool | list[bool] = True) -> list[float]:
        """core.select.ZenzScorer.logprobs と同じ（続きのトークンの対数確率の和）。
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
            plen = self._plan(chunk)
            pk, pv, pmask = self._assemble([s for s, _ in chunk], plen)
            # 前置きの後ろは右に詰める。位置は行の前置きの長さから続ける
            B = len(chunk)
            S = max(len(s) - n for (s, _), n in zip(chunk, plen))
            ids = np.full((B, S), self.pad, dtype=np.int64)
            smask = np.zeros((B, S), dtype=np.int64)
            for r, ((s, _), n) in enumerate(zip(chunk, plen)):
                ids[r, :len(s) - n] = s[n:]
                smask[r, :len(s) - n] = 1
            pos = np.array(plen, dtype=np.int64)[:, None] + np.arange(S, dtype=np.int64)
            logits = self._run(ids, pos, np.concatenate([pmask, smask], axis=1), pk, pv)[0]
            for r, ((s, n_p), n) in enumerate(zip(chunk, plen)):
                lp = _log_softmax(logits[r, n_p - 1 - n:len(s) - 1 - n].astype(np.float32))
                out.append(float(lp[np.arange(len(s) - n_p), s[n_p:]].sum()))
        return out

    def fluency(self, texts: list[str], left_context: str = "") -> list[float]:
        from eval.prompts import CONTEXT, INPUT
        return self.logprobs([CONTEXT + left_context] * len(texts), [scoring_text(t) + INPUT for t in texts], eos=False)


class OrtCorePipeline(CorePipeline):
    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", *, threads: int | None = None,
                 onnx_path: str | Path | None = None, name: str | None = None, share: bool = True, **kwargs):
        super().__init__(name=name or "core-ort", backend=OrtBackend(model, onnx_path=onnx_path, threads=threads,
                                                                     share=share), **kwargs)
