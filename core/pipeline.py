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


class CorePipeline:
    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", device: str = "cpu", k: int = 3,
                 n_read: int = 20, w_read: float = 1.0, w_flu: float = 0.0, name: str | None = None):
        self.dic = Dictionary.load()
        self.scorer = ZenzScorer(model, device)
        self.cz = ConstrainedZenz(self.scorer)
        self.k, self.n_read, self.w_read, self.w_flu = k, n_read, w_read, w_flu
        self.name = name or f"core(k={k},w_read={w_read})"
        self.n_params = sum(p.numel() for p in self.scorer.model.parameters())

    def candidates(self, romaji: str, left_context: str = "") -> list[dict]:
        """読みの上位 k 件の変換と点数（点数の高い順）。"""
        out = []
        rs = readings(romaji, n=self.n_read)[: self.k]
        if rs and all(r.edits for r in rs):
            # 打鍵を直さない読みも必ず比べる。辞書に無い略語・型番（cdma, jr, scc-0210）は、読みの段では
            # 直す経路のほうが安くなりやすい。直すかどうかは zenz の尤度との比較に任せる
            rs += readings(romaji, n=1, max_edits=0)
        for r in rs:
            t0 = time.perf_counter()
            text, lp, finished = self.cz.decode_scored(r, self.dic, left_context)
            out.append({
                "text": text, "score": lp - self.w_read * r.cost if finished else float("-inf"),
                "lm": lp, "read_cost": r.cost, "reading": r.zenz_input(), "edits": [e.detail for e in r.edits],
                "ms": (time.perf_counter() - t0) * 1000,
            })
        if self.w_flu and out:
            # 完成文としての自然さを足す（core.select.fluency）。zenz の変換の確率は読みの良し悪しを比べられないため
            for c, f in zip(out, fluency(self.scorer, [c["text"] for c in out], left_context)):
                c["fluency"] = f
                c["score"] += self.w_flu * f
        return sorted(out, key=lambda c: -c["score"])

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result:
        cands = self.candidates(romaji, left_context)
        if not cands:
            return Result("", prompt=romaji)
        best = cands[0]
        return Result(best["text"], prompt=best["reading"], candidates=tuple(c["text"] for c in cands),
                      edited=bool(best["edits"]))

    def convert(self, romaji: str, left_context: str = "") -> str:
        return self.convert_detailed(romaji, left_context).text
