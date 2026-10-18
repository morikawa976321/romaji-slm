"""変換コアの生成と採点を llama-server で行う（docs/LLAMA_BACKEND.md）。

core.constrained.ConstrainedZenz（transformers）と同じ量を返す:

- decode_batch: 辞書ラティスを GBNF 文法（core.gbnf）にして `/completion` に渡し、貪欲法で生成させる。
  llama-server の `n_probs` は既定で文法をかける前の分布の確率を返すので、選んだトークンの logprob の和が
  ConstrainedZenz の logp（制約前の分布で測る）と同じ量になる。
- fluency: 完成文を1本の文字列の文法で強制生成させ、その logprob の和を取る（core.select.fluency と同じ量）。

読みの k 件は、llama-server のスロット（`-np`）の数まで並列に投げる。
"""

import http.client
import json
import math
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eval.prompts import CONTEXT, INPUT
from infer.llama import LlamaServer

from .constrained import Decoded, LatticeMatcher
from .gbnf import DEAD_END, bounds_from_text, gbnf_literal, grammar_size, matcher_grammar, matcher_grammar_stop
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
                 extra_args: list[str] | None = None, dead_ends: str = "prune", fluency_mode: str = "gen",
                 refill: str = "full", keep_alive: bool = False):
        """変種（docs/LLAMA_PARITY.md。既定はどれも従来の振る舞い）:

        dead_ends: "prune" = 文法で行き止まりを刈る（従来）。"stop" = 刈らずに、行き止まりに入ったら未完にする
          （torch 版 ConstrainedZenz と同じ探索）。
        fluency_mode: "gen" = 文字列の文法で強制生成し、分割が正規と違えば位置ごとに取り直す（従来）。
          "tok" = 正規のトークン列をトークン ID の文法（`<[id]>`）で強制する（分割の取り直しが起きない）。
        refill: "full" = 載らなかった logprob を1位置1要求・語彙全体で取り直す（従来）。"fast" = 1候補の全位置を
          1要求（プロンプトの配列）にまとめ、まず上位 64 で探し、無ければ語彙全体で取り直す。トークン化も覚えておく。
          "eos" = "fast" に加えて、自然さ（fluency_mode="tok" のとき）は文の後ろの EOS の項目の上位から入力開始の印の
          1 バイト目の確率を取り、2・3 バイト目は 0 とみなす（torch 版の実測で |logprob| < 1e-4。取り直しの要求が要らない）。
        keep_alive: HTTP の接続をスレッドごとに使い回す（urllib は要求ごとに接続し直す）。
        """
        if dead_ends not in ("prune", "stop") or fluency_mode not in ("gen", "tok") or refill not in ("full", "fast", "eos"):
            raise ValueError(f"変種の指定が想定外: {dead_ends=} {fluency_mode=} {refill=}")
        self.server = LlamaServer(model, url=url, bin_dir=bin_dir, threads=threads, n_gpu_layers=n_gpu_layers,
                                  extra_args=extra_args, n_slots=n_slots)
        self.n_slots = n_slots
        self.dead_ends, self.fluency_mode, self.refill, self.keep_alive = dead_ends, fluency_mode, refill, keep_alive
        self._local = threading.local()
        self._tok_cache: dict[str, list[int]] = {}
        self.http: list[dict] = []  # 1 HTTP 要求ごとの記録（速度の内訳。eval/parity_speed.py が読む）
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
        # 行き止まりの印は、ほかに許すトークンが無いときだけ選ばれるようにする（dead_ends="stop"）
        dead = self.tokenize("\x7f")
        self.dead_id = dead[0] if len(dead) == 1 else None
        self.ban_dead = self.ban + ([[self.dead_id, -1000.0]] if self.dead_id is not None else [])
        self.n_dead = 0  # 行き止まりで止まった生成の数
        self.input_ids = self.tokenize(INPUT)
        self.eos_logprob_included: bool | None = None  # EOS で止まった最初の応答で決まる（docs に記録する事実）
        self.n_refill = 0  # 載らなかった logprob を取り直した回数
        self.n_resplit = 0  # 自然さの強制生成が正規の分割にならず、位置ごとに取り直した回数
        self.n_refill_wide = 0  # refill="fast" で上位に無く、語彙全体で取り直した回数
        self.log: list[dict] = []  # 1要求ごとの記録（文法の大きさ・timings）。eval/bench_backend.py が読む

    def _post(self, path: str, body: dict, kind: str) -> dict | list:
        """POST して JSON を返す。所要時間と大きさを self.http に記録する。"""
        t0 = time.perf_counter()
        data = json.dumps(body).encode()
        if self.keep_alive:
            # サーバーが使い回しの接続を閉じていることがある（WinError 10053）。1 回だけ繋ぎ直して送り直す
            for attempt in (0, 1):
                conn = getattr(self._local, "conn", None)
                if conn is None:
                    u = urllib.parse.urlsplit(self.server.url)
                    conn = self._local.conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
                try:
                    conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    raw = resp.read()
                    break
                except (http.client.HTTPException, ConnectionError) as e:
                    conn.close()
                    self._local.conn = None
                    if attempt:
                        raise RuntimeError(f"llama-server に繋ぎ直しても送れなかった: {path}") from e
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {path} {raw[:500]!r}")
        else:
            req = urllib.request.Request(self.server.url + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        t1 = time.perf_counter()
        out = json.loads(raw.decode())
        t2 = time.perf_counter()
        res = out if isinstance(out, list) else [out]
        tms = [x.get("timings") or {} for x in res if isinstance(x, dict)]
        self.http.append({"kind": kind, "ms": (t2 - t0) * 1000, "parse_ms": (t2 - t1) * 1000,
                          "req_bytes": len(data), "resp_bytes": len(raw), "n_prompts": len(res),
                          "prompt_ms": sum(t.get("prompt_ms", 0) for t in tms),
                          "predicted_ms": sum(t.get("predicted_ms", 0) for t in tms),
                          "prompt_n": sum(t.get("prompt_n", 0) for t in tms),
                          "predicted_n": sum(t.get("predicted_n", 0) for t in tms)})
        return out

    def tokenize(self, text: str) -> list[int]:
        """`/completion` と同じ条件（特殊トークンを足す・解釈する）でトークン化する。"""
        if self.refill != "full" and text in self._tok_cache:
            return self._tok_cache[text]
        ids = self._post("/tokenize", {"content": text, "add_special": True, "parse_special": True},
                         "tokenize")["tokens"]
        if self.refill != "full":
            self._tok_cache[text] = ids
        return ids

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
                  ban: list | None = None, n_probs: int = 1) -> tuple[dict, list[float]]:
        """制約付きで貪欲生成し、(応答, 生成した各トークンの logprob) を返す。"""
        t0 = time.perf_counter()
        r = self._post("/completion", {
            "prompt": prompt, "grammar": grammar, "n_predict": n_predict, "temperature": 0, "top_k": 1,
            "samplers": ["top_k"], "n_probs": n_probs, "cache_prompt": True, "return_tokens": True,
            "logit_bias": self.ban if ban is None else ban,
        }, kind)
        lps = self._token_logprobs(prompt, r)
        rules, nbytes = grammar_size(grammar)
        self.log.append({"kind": kind, "ms": (time.perf_counter() - t0) * 1000, "grammar_rules": rules,
                         "grammar_bytes": nbytes, "timings": r.get("timings") or {}, "prompt": prompt})
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
            ids = self.tokenize(prompt)
            if self.refill != "full":
                lps = self._logprobs_at([ids + toks[:i] for i in missing], [toks[i] for i in missing])
                for i, lp in zip(missing, lps):
                    out[i] = lp
            else:
                for i in missing:
                    out[i] = self._logprob_at(ids + toks[:i], toks[i])
        return out

    def _logprob_at(self, ids: list[int], tok: int) -> float:
        """トークン列 ids の次に tok が来る対数確率（制約前）。語彙全体の確率を取る。"""
        self.n_refill += 1
        r = self._post("/completion", {
            "prompt": ids, "n_predict": 1, "grammar": _ONLY_EOG, "n_probs": self.n_vocab, "temperature": 0,
            "cache_prompt": True, "logit_bias": self.ban}, "refill")
        for e in r["completion_probabilities"][0]["top_logprobs"]:
            if e["id"] == tok:
                return entry_logprob(e)
        raise RuntimeError(f"トークン {tok} の確率が返らなかった")

    _REFILL_TOP = 64  # refill="fast": まずこの上位で探す

    def _logprobs_at(self, prefixes: list[list[int]], toks: list[int]) -> list[float]:
        """_logprob_at の複数位置版。1要求（プロンプトの配列）で上位 _REFILL_TOP を取り、無い位置だけ語彙全体で取る。"""
        self.n_refill += len(toks)
        rs = self._post("/completion", {
            "prompt": prefixes, "n_predict": 1, "grammar": _ONLY_EOG, "n_probs": self._REFILL_TOP,
            "temperature": 0, "cache_prompt": True, "logit_bias": self.ban}, "refill")
        rs = sorted(rs if isinstance(rs, list) else [rs], key=lambda r: r.get("index", 0))
        out = []
        for ids, tok, r in zip(prefixes, toks, rs):
            hit = [e for e in r["completion_probabilities"][0]["top_logprobs"] if e["id"] == tok]
            if hit:
                out.append(entry_logprob(hit[0]))
            else:
                self.n_refill -= 1
                self.n_refill_wide += 1
                out.append(self._logprob_at(ids, tok))
        return out

    def _decode_one(self, reading: Reading, dic: Dictionary, left_context: str, max_new: int | None,
                    punct: str, en_case: str) -> Decoded:
        m = LatticeMatcher(reading, dic, punct, en_case)
        stop = self.dead_ends == "stop" and self.dead_id is not None
        g = matcher_grammar_stop(m) if stop else matcher_grammar(m)
        if g is None:
            # ラティスに終点まで届く経路が無い（ConstrainedZenz では最初の1手で候補が尽きるのと同じ）
            return Decoded("", 0.0, False, ())
        n_predict = max_new or 3 * len(reading.zenz_input()) + 8
        r, lps = self._complete(zenz_prompt(reading.zenz_input(), left_context), g, n_predict, "decode",
                                self.ban_dead if stop else None)
        text = r.get("content", "")
        toks = r.get("tokens") or []
        if stop and self.dead_id in toks:
            # 行き止まりに入った（torch 版では許すトークンが尽きて止まる）。印から後ろは捨てる
            self.n_dead += 1
            text = text.split("\x7f")[0]
            return Decoded(text, sum(lps[:toks.index(self.dead_id)]), False, bounds_from_text(m, text))
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
        canon = self.tokenize(target) if self.tok_bytes is not None else None
        n_in = len(self.input_ids)
        if self.refill == "eos" and self.fluency_mode == "tok" and canon and canon[-n_in:] == self.input_ids:
            return self._fluency_eos(prompt, canon[:-n_in])
        if self.fluency_mode == "tok" and canon:
            # 正規のトークン列を ID の文法で強制する。字の途中のバイトのトークン（入力開始の印など）だけは文字で書く
            # （b10549 は `<[id]>` で字の途中のバイトを受理すると、その後の EOG で 500 を返した）
            grammar, ban = self._token_grammar(canon), None
        else:
            # 正規の分割に字の途中のバイトが無ければ、それを禁じて強制生成の分割を揃える
            ban = self.ban_partial if canon is not None and not self.partial.intersection(canon) else None
            grammar = f"root ::= {gbnf_literal(target.encode())}\n"
        # 生成するトークン数 ≤ バイト数（バイト単位 BPE）。最後に EOS が1つ来る
        r, lps = self._complete(prompt, grammar, len(target.encode()) + 2, "fluency", ban)
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
            ids = self.tokenize(prompt)
            return sum(self._logprob_at(ids + canon[:i], t) for i, t in enumerate(canon))
        return sum(lps)

    _EOS_TOP = 16  # refill="eos": EOS の項目に載せる上位の数（印の1バイト目は torch 版の実測で最悪 9 位）

    def _fluency_eos(self, prompt: str, canon: list[int]) -> float:
        """文の正規のトークン列だけを強制し、EOS の項目の上位から印の1バイト目の logprob を取る。"""
        grammar = self._token_grammar(canon) if canon else _ONLY_EOG
        r, lps = self._complete(prompt, grammar, len(canon) + 1, "fluency", None, self._EOS_TOP)
        toks = r.get("tokens") or []
        if toks[:len(canon)] != canon:
            self.n_resplit += 1
            ids = self.tokenize(prompt)
            return sum(self._logprob_at(ids + canon[:i], t) for i, t in enumerate(canon + self.input_ids))
        last = (r.get("completion_probabilities") or [{}])[-1].get("top_logprobs") or []
        hit = [e for e in last if e["id"] == self.input_ids[0]] if len(toks) == len(canon) + 1 else []
        if hit:
            lp1 = entry_logprob(hit[0])
        else:
            self.n_refill_wide += 1
            lp1 = self._logprob_at(self.tokenize(prompt) + canon, self.input_ids[0])
        return sum(lps[:len(canon)]) + lp1

    def _token_grammar(self, canon: list[int]) -> str:
        parts, run = [], bytearray()
        for t in canon:
            if t not in self.partial and not run:
                parts.append(f"<[{t}]>")
                continue
            run += self.tok_bytes[t]
            if _valid_utf8(bytes(run)):
                parts.append(gbnf_literal(bytes(run)))
                run.clear()
        if run:
            raise RuntimeError(f"正規の分割が UTF-8 として閉じない: {canon}")
        return "root ::= " + " ".join(parts) + "\n"

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
                 name: str | None = None, dead_ends: str = "prune", fluency_mode: str = "gen", refill: str = "full",
                 keep_alive: bool = False, extra_args: list[str] | None = None, **kwargs):
        backend = LlamaBackend(model, url=url, n_slots=n_slots, threads=threads, bin_dir=bin_dir,
                               n_gpu_layers=n_gpu_layers, extra_args=extra_args, dead_ends=dead_ends,
                               fluency_mode=fluency_mode, refill=refill, keep_alive=keep_alive)
        super().__init__(name=name or f"core-llama({Path(model).name if model else url})", backend=backend,
                         **kwargs)

    def close(self) -> None:
        self.backend.close()
