"""変換コアの生成と採点を llama-server で行う（docs/LLAMA_BACKEND.md）。

core.constrained.ConstrainedZenz（transformers）と同じ量を返す:

- decode_batch: 辞書ラティスを GBNF 文法（core.gbnf）にして `/completion` に渡し、貪欲法で生成させる。
  llama-server の `n_probs` は既定で文法をかける前の分布の確率を返すので、選んだトークンの logprob の和が
  ConstrainedZenz の logp（制約前の分布で測る）と同じ量になる。
- fluency: 完成文を1本の文字列の文法で強制生成させ、その logprob の和を取る（core.select.fluency と同じ量）。

読みの k 件は、llama-server のスロット（`-np`）の数まで並列に投げる。
"""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eval.prompts import CONTEXT, INPUT
from infer.llama import LlamaServer

from .constrained import Decoded, LatticeMatcher
from .gbnf import bounds_from_text, gbnf_literal, grammar_size, matcher_grammar
from .kanakanji import Dictionary
from .pipeline import CorePipeline
from .select import scoring_text, zenz_prompt
from .types import Reading

__all__ = ["LlamaBackend", "LlamaCorePipeline", "LlamaServer"]


def entry_logprob(e: dict) -> float:
    """`completion_probabilities` の1項目の logprob（制約前の分布）。

    版により形が違う: `{"id","token","bytes","logprob","top_logprobs"}`（post_sampling_probs が偽、新しい版の既定）と
    `{"token","prob","top_probs"}`（post_sampling_probs が真、または古い版）。後者は確率なので対数にする。
    """
    if "logprob" in e:
        return float(e["logprob"])
    if "prob" in e:
        return math.log(e["prob"]) if e["prob"] > 0 else float("-inf")
    raise ValueError(f"completion_probabilities の形が想定外: {sorted(e)}")


def utf8_complete(b: bytes) -> bool:
    """末尾が UTF-8 の文字の途中で切れていないか（llama-server の validate_utf8 と同じ判定）。"""
    for i in range(1, min(4, len(b)) + 1):
        c = b[-i]
        if c & 0xC0 == 0x80:
            continue
        need = 1 if c < 0x80 else 2 if c >> 5 == 0b110 else 3 if c >> 4 == 0b1110 else 4 if c >> 3 == 0b11110 else 1
        return i >= need
    return True


def _valid_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _stopped_eos(resp: dict) -> bool:
    return resp.get("stop_type") == "eos" or bool(resp.get("stopped_eos"))


_ONLY_EOG = 'root ::= ""\n'  # 文法の終わり = EOG しか選べない


class LlamaBackend:
    """CorePipeline に注入する生成と採点（decode_batch・fluency）。

    llama-server の2つの癖を吸収する（docs/LLAMA_BACKEND.md「HF 版との差」）:

    - `[PAD]` が EOG 扱いになる版がある（FIM_PAD の名前の表に `[PAD]` が入っている。62df181 で確認）。
      文法の終点で `</s>` の代わりに `[PAD]` が選ばれうるので、EOS 以外の表層の空なトークンを logit_bias で禁じる
      （ConstrainedZenz も表層の空なトークンは EOS 以外選ばない）。logit_bias は n_probs の値（制約前）を変えない。
    - UTF-8 の文字の途中で終わるトークン（語彙に無い字のバイト）は `completion_probabilities` に載らない
      （文字が完成するトークンだけが載る）。載らなかった位置は、そこまでのトークン列をプロンプトにして
      語彙全体の確率を1回取り直して埋める（zenz の語彙は1文字1トークンなので、語彙に無い字のときだけ起きる）。
    """

    def __init__(self, model: str | Path | None = None, *, url: str | None = None, n_slots: int = 5,
                 threads: int | None = None, bin_dir: str | Path | None = None, n_gpu_layers: int | None = None,
                 extra_args: list[str] | None = None):
        self.server = LlamaServer(model, url=url, bin_dir=bin_dir, threads=threads, n_gpu_layers=n_gpu_layers,
                                  extra_args=extra_args, n_slots=n_slots)
        self.n_slots = n_slots
        self.pool = ThreadPoolExecutor(max_workers=n_slots)
        models = self.server._request("GET", "/v1/models").get("data") or [{}]
        meta = models[0].get("meta") or {}
        self.n_params = int(meta.get("n_params") or 0)
        self.n_vocab = int(meta.get("n_vocab") or 0)
        props = self.server._request("GET", "/props")
        eos = self.server.tokenize(props.get("eos_token") or "</s>")
        self.eos = eos[0] if len(eos) == 1 else None
        self.tok_bytes = self._vocab_bytes()
        # EOS 以外の、表層が空のトークン（特殊トークン）を禁じる
        self.ban = [[i, False] for i, b in enumerate(self.tok_bytes or []) if not b and i != self.eos]
        # 単独では UTF-8 として完結しないトークン（字の途中のバイト）。強制生成の分割を正規の分割に揃えるのに使う
        self.partial = {i for i, b in enumerate(self.tok_bytes or []) if b and not _valid_utf8(b)}
        self.ban_partial = self.ban + [[i, False] for i in sorted(self.partial)]
        self.eos_logprob_included: bool | None = None  # EOS で止まった最初の応答で決まる（docs に記録する事実）
        self.n_refill = 0  # 載らなかった logprob を取り直した回数
        self.n_resplit = 0  # 自然さの強制生成が正規の分割にならず、位置ごとに取り直した回数
        self.log: list[dict] = []  # 1要求ごとの記録（文法の大きさ・timings）。eval/bench_backend.py が読む

    def _vocab_bytes(self) -> list[bytes] | None:
        """トークン ID → バイト列。語彙全体の確率を1回取り、各項目の `bytes` から作る。古い版で無ければ None。"""
        if not self.n_vocab:
            return None
        r = self.server._request("POST", "/completion", {
            "prompt": INPUT, "n_predict": 1, "grammar": _ONLY_EOG, "n_probs": self.n_vocab, "temperature": 0})
        top = (r.get("completion_probabilities") or [{}])[0].get("top_logprobs") or []
        if len(top) != self.n_vocab or any("bytes" not in t for t in top):
            return None
        out = [b""] * self.n_vocab
        for t in top:
            out[t["id"]] = bytes(t["bytes"])
        return out

    def _complete(self, prompt: str, grammar: str, n_predict: int, kind: str,
                  ban: list | None = None) -> tuple[dict, list[float]]:
        """制約付きで貪欲生成し、(応答, 生成した各トークンの logprob) を返す。"""
        t0 = time.perf_counter()
        r = self.server._request("POST", "/completion", {
            "prompt": prompt, "grammar": grammar, "n_predict": n_predict, "temperature": 0, "top_k": 1,
            "samplers": ["top_k"], "n_probs": 1, "cache_prompt": True, "return_tokens": True,
            "logit_bias": self.ban if ban is None else ban,
        })
        lps = self._token_logprobs(prompt, r)
        rules, nbytes = grammar_size(grammar)
        self.log.append({"kind": kind, "ms": (time.perf_counter() - t0) * 1000, "grammar_rules": rules,
                         "grammar_bytes": nbytes, "timings": r.get("timings") or {}})
        return r, lps

    def _token_logprobs(self, prompt: str, r: dict) -> list[float]:
        entries = r.get("completion_probabilities") or []
        toks = r.get("tokens")
        if self.tok_bytes is None or toks is None:
            # 古い版: 載った分だけを足す（語彙に無い字のバイトの分が抜ける）
            if _stopped_eos(r):
                self.eos_logprob_included = len(entries) == int(r.get("tokens_predicted", -1))
            return [entry_logprob(e) for e in entries]
        out: list[float | None] = []
        buf = bytearray()
        k = 0
        for t in toks:
            buf += self.tok_bytes[t]
            if utf8_complete(buf) and k < len(entries):
                if entries[k].get("id", t) != t:
                    raise RuntimeError(f"completion_probabilities とトークン列が対応しない: {entries[k]} / {t}")
                out.append(entry_logprob(entries[k]))
                k += 1
            else:
                out.append(None)
        if toks and toks[-1] == self.eos and _stopped_eos(r):
            self.eos_logprob_included = out[-1] is not None
        missing = [i for i, lp in enumerate(out) if lp is None]
        if missing:
            ids = self.server.tokenize(prompt)
            for i in missing:
                out[i] = self._logprob_at(ids + toks[:i], toks[i])
        return out

    def _logprob_at(self, ids: list[int], tok: int) -> float:
        """トークン列 ids の次に tok が来る対数確率（制約前）。語彙全体の確率を取る。"""
        self.n_refill += 1
        r = self.server._request("POST", "/completion", {
            "prompt": ids, "n_predict": 1, "grammar": _ONLY_EOG, "n_probs": self.n_vocab, "temperature": 0,
            "cache_prompt": True, "logit_bias": self.ban})
        for e in r["completion_probabilities"][0]["top_logprobs"]:
            if e["id"] == tok:
                return entry_logprob(e)
        raise RuntimeError(f"トークン {tok} の確率が返らなかった")

    def _decode_one(self, reading: Reading, dic: Dictionary, left_context: str, max_new: int | None,
                    punct: str, en_case: str) -> Decoded:
        m = LatticeMatcher(reading, dic, punct, en_case)
        g = matcher_grammar(m)
        if g is None:
            # ラティスに終点まで届く経路が無い（ConstrainedZenz では最初の1手で候補が尽きるのと同じ）
            return Decoded("", 0.0, False, ())
        n_predict = max_new or 3 * len(reading.zenz_input()) + 8
        r, lps = self._complete(zenz_prompt(reading.zenz_input(), left_context), g, n_predict, "decode")
        text = r.get("content", "")
        return Decoded(text, sum(lps), _stopped_eos(r), bounds_from_text(m, text))

    def decode_batch(self, readings: list[Reading], dic: Dictionary, left_context: str = "",
                     max_new: int | None = None, punct: str = "auto", en_case: str = "zenz") -> list[Decoded]:
        """ConstrainedZenz.decode_batch と同じ口。読みごとに1要求を作り、スロットの数まで並列に投げる。"""
        return list(self.pool.map(lambda r: self._decode_one(r, dic, left_context, max_new, punct, en_case),
                                  readings))

    def _fluency_one(self, text: str, left_context: str) -> float:
        """core.select.fluency は文字列を正規のトークン列（トークナイザの分割）で採点する。強制生成は文字列が同じでも
        別の分割（字をバイトのトークンで出すなど）を選びうるので、正規の分割と照合し、違えば位置ごとに取り直す。"""
        target = scoring_text(text) + INPUT
        prompt = CONTEXT + left_context
        canon = self.server.tokenize(target) if self.tok_bytes is not None else None
        # 正規の分割に字の途中のバイトが無ければ、それを禁じて強制生成の分割を揃える
        ban = self.ban_partial if canon is not None and not self.partial.intersection(canon) else None
        # 生成するトークン数 ≤ バイト数（バイト単位 BPE）。最後に EOS が1つ来る
        r, lps = self._complete(prompt, f"root ::= {gbnf_literal(target.encode())}\n", len(target.encode()) + 2,
                                "fluency", ban)
        if r.get("content", "") != target:
            raise RuntimeError(f"強制生成が文字列を出し切らなかった: {target!r} → {r.get('content')!r}")
        toks = r.get("tokens") or []
        if self.tok_bytes is not None and toks:
            if toks[-1] == self.eos:
                toks, lps = toks[:-1], lps[:-1]
        elif _stopped_eos(r) and self.eos_logprob_included:
            lps = lps[:-1]
        if canon is not None and toks != canon:
            self.n_resplit += 1
            ids = self.server.tokenize(prompt)
            return sum(self._logprob_at(ids + canon[:i], t) for i, t in enumerate(canon))
        return sum(lps)

    def fluency(self, texts: list[str], left_context: str = "") -> list[float]:
        """core.select.fluency と同じ量（EOS は含めない）。"""
        return list(self.pool.map(lambda t: self._fluency_one(t, left_context), texts))

    def close(self) -> None:
        self.pool.shutdown(wait=False)
        self.server.close()


class LlamaCorePipeline(CorePipeline):
    """CorePipeline の生成と採点を llama-server に替えたもの（点数の式・句読点の挿入は同じ）。"""

    def __init__(self, model: str | Path | None = None, *, url: str | None = None, n_slots: int = 5,
                 threads: int | None = None, bin_dir: str | Path | None = None, n_gpu_layers: int | None = None,
                 name: str | None = None, **kwargs):
        backend = LlamaBackend(model, url=url, n_slots=n_slots, threads=threads, bin_dir=bin_dir,
                               n_gpu_layers=n_gpu_layers)
        super().__init__(name=name or f"core-llama({Path(model).name if model else url})", backend=backend,
                         **kwargs)

    def close(self) -> None:
        self.backend.close()
