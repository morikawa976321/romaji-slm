"""変換コアの全体: 打鍵列 → 読みの候補（core.reading）→ 辞書ラティスで制約した zenz の生成（core.constrained）→ 1候補。

読みの上位 k 件それぞれについて制約付き生成を行い、

    score = zenz の対数確率（出力 + EOS）+ w_flu × 完成文の自然さ − w_read × 読みのコスト

が最大のものを返す。英語か日本語か・打鍵ミスを直すかは、ここで zenz の尤度と読みのコストの比較で決まる。
"""

import time

from eval.converters import Result

from .constrained import ConstrainedZenz
from .kanakanji import Dictionary
from .reading import readings
from .select import ZenzScorer, fluency


_TYPED_PUNCT = frozenset(",.?!")  # 打鍵列に句読点として打たれうる記号
_PUNCT = frozenset("、。？！?!,.")


def punct_variants(text: str, bounds: tuple[int, ...]) -> list[tuple[str, int, int]]:
    """句読点 B の候補: (文, 読点を足した数, 句点を足した数)。無挿入を必ず含む。

    読点は語の切れ目（制約付き生成が通ったラティスの節点）に1つまで、句点は末尾に句読点が無いときだけ。
    """
    if not text:
        return [(text, 0, 0)]
    ends = [""] + (["。"] if text[-1] not in _PUNCT else [])
    commas: list[int | None] = [None] + [b for b in bounds if 0 < b < len(text)
                                         and text[b - 1] not in _PUNCT and text[b] not in _PUNCT]
    out = []
    for e in ends:
        for b in commas:
            t = text if b is None else text[:b] + "、" + text[b:]
            out.append((t + e, int(b is not None), int(bool(e))))
    return out


class CorePipeline:
    # 既定値は user_style・gen_test・AJIMEE で比べて決めた（docs/EVAL.md「完成文の自然さ」。w_read/w_flu の比が効く）
    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", device: str = "cpu", k: int = 5,
                 n_read: int = 20, w_read: float = 4.0, w_flu: float = 1.0, name: str | None = None,
                 batched: bool = True, punct: str = "auto", en_case: str = "zenz",
                 tau_comma: float = 0.0, tau_end: float = 0.0, backend=None):
        """punct / en_case: 記号と英字の表記モード（core.constrained.LatticeMatcher）。punct="insert" は
        句読点の自動挿入（句読点 B）: 打鍵列に句読点が無いとき、完成文の自然さで読点・句点を補う。
        tau_comma / tau_end: 挿入1つあたりに自然さから引く閾値（大きいほど挿入しにくい）。
        backend: 生成と採点の差し替え（core.llama_backend.LlamaBackend）。decode_batch と fluency を持つもの。
        None なら model を transformers で読む。
        """
        self.dic = Dictionary.load()
        self.backend = backend
        if backend is None:
            self.scorer = ZenzScorer(model, device)
            self.cz = ConstrainedZenz(self.scorer)
        self.k, self.n_read, self.w_read, self.w_flu = k, n_read, w_read, w_flu
        self.batched = batched  # k 件の制約付き生成を1バッチで回す（False は1件ずつ。速度の比較用）
        self.punct, self.en_case = punct, en_case
        self.tau_comma, self.tau_end = tau_comma, tau_end
        self.name = name or f"core(k={k},w_read={w_read})"
        if backend is not None:
            self.n_params = backend.n_params
        else:
            self.n_params = sum(p.numel() for p in self.scorer.model.parameters())

    def candidates(self, romaji: str, left_context: str = "") -> list[dict]:
        """読みの上位 k 件の変換と点数（点数の高い順）。"""
        out = []
        rs = readings(romaji, n=self.n_read)[: self.k]
        if rs and all(r.edits for r in rs):
            # 打鍵を直さない読みも必ず比べる。辞書に無い略語・型番（cdma, jr, scc-0210）は、読みの段では
            # 直す経路のほうが安くなりやすい。直すかどうかは zenz の尤度との比較に任せる
            rs += readings(romaji, n=1, max_edits=0)
        t0 = time.perf_counter()
        modes = dict(punct=self.punct, en_case=self.en_case)
        if self.backend is not None:
            decoded = self.backend.decode_batch(rs, self.dic, left_context, **modes)
        elif self.batched:
            decoded = self.cz.decode_batch(rs, self.dic, left_context, **modes)
        else:
            decoded = [self.cz.decode_scored(r, self.dic, left_context, **modes) for r in rs]
        decode_ms = (time.perf_counter() - t0) * 1000
        for r, d in zip(rs, decoded):
            out.append({
                "text": d.text, "score": d.logp - self.w_read * r.cost if d.finished else float("-inf"),
                "lm": d.logp, "read_cost": r.cost, "reading": r.zenz_input(), "edits": [e.detail for e in r.edits],
                "bounds": d.bounds,
                "ms": decode_ms,  # k 件まとめての生成時間（候補ごとには分けられない）
            })
        if self.w_flu and out:
            # 完成文としての自然さを足す（core.select.fluency）。zenz の変換の確率は読みの良し悪しを比べられないため
            for c, f in zip(out, self._fluency([c["text"] for c in out], left_context)):
                c["fluency"] = f
                c["score"] += self.w_flu * f
        return sorted(out, key=lambda c: -c["score"])

    def _fluency(self, texts: list[str], left_context: str) -> list[float]:
        if self.backend is not None:
            return self.backend.fluency(texts, left_context)
        return fluency(self.scorer, texts, left_context)

    def insert_punct(self, text: str, bounds: tuple[int, ...], left_context: str = "") -> str:
        """句読点 B: punct_variants の候補を完成文の自然さで比べ、閾値を引いて最良のものを返す。"""
        vs = punct_variants(text, bounds)
        if len(vs) == 1:
            return text
        fl = self._fluency([v[0] for v in vs], left_context)
        best = max(zip(vs, fl), key=lambda vf: vf[1] - self.tau_comma * vf[0][1] - self.tau_end * vf[0][2])
        return best[0][0]

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result:
        cands = self.candidates(romaji, left_context)
        if not cands:
            return Result("", prompt=romaji)
        best = cands[0]
        text = best["text"]
        if self.punct == "insert" and not (_TYPED_PUNCT & set(romaji)) and best["score"] != float("-inf"):
            # 句読点を打った入力には足さない（打った句読点の種類と幅だけ決める = 句読点 A と同じ振る舞い）
            text = self.insert_punct(text, best["bounds"], left_context)
        return Result(text, prompt=best["reading"], candidates=tuple(c["text"] for c in cands),
                      edited=bool(best["edits"]))

    def convert(self, romaji: str, left_context: str = "") -> str:
        return self.convert_detailed(romaji, left_context).text
