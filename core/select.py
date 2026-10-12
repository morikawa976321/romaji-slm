"""工程4: 変換候補（core.kanakanji の N-best）から、凍結した zenz の対数尤度で1つを選ぶ（docs/REDESIGN.md）。

点数（大きいほど良い）:

    score = zenz の log P(表層 + EOS | 左文脈・読み)
            - w_dict × 辞書・接続コスト / 500
            - w_read × 前段（読み）のコスト

Mozc のコストは −500·log(確率) の尺度なので、500 で割って対数確率の尺度に揃える。重みは開発用データで決める
初期値で、確率として厳密に同じ尺度ではない。zenz は生成させず、候補の表層を与えて採点する（teacher forcing）。
表層は辞書・かな・原文コピーでできているので、出力が読みで説明できないことは構造上起きない。
"""

from dataclasses import dataclass

from eval.prompts import CONTEXT, INPUT, OUTPUT

from .types import Conversion

MOZC_COST_SCALE = 500.0


def zenz_prompt(zenz_input: str, left_context: str = "") -> str:
    head = CONTEXT + left_context if left_context else ""
    return f"{head}{INPUT}{zenz_input}{OUTPUT}"


def scoring_text(surface: str) -> str:
    """zenz に採点させる文字列。zenz のトークナイザは空白を落とすので、空白を除いて採点する。表示は元の表層。"""
    return surface.replace(" ", "")


class ZenzScorer:
    """元の zenz（Hugging Face 形式）で、プロンプトに続く文字列 + EOS の対数尤度を計算する。"""

    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", device: str = "cpu", batch: int = 32):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self.device = device
        self.batch = batch
        self.tok = AutoTokenizer.from_pretrained(model)
        self.model = AutoModelForCausalLM.from_pretrained(model, dtype=torch.float32).to(device).eval()
        self.pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id

    def logprobs(self, prompts: list[str], continuations: list[str]) -> list[float]:
        """各 i について log P(continuations[i] + EOS | prompts[i]) を返す（トークンの対数確率の和）。"""
        torch, tok = self._torch, self.tok
        seqs = []
        for p, c in zip(prompts, continuations):
            p_ids = tok(p, add_special_tokens=False).input_ids
            c_ids = tok(c, add_special_tokens=False).input_ids + [tok.eos_token_id]
            seqs.append((p_ids + c_ids, len(p_ids)))
        out: list[float] = []
        for k in range(0, len(seqs), self.batch):
            chunk = seqs[k:k + self.batch]
            L = max(len(s) for s, _ in chunk)
            ids = torch.full((len(chunk), L), self.pad, dtype=torch.long)
            mask = torch.zeros((len(chunk), L), dtype=torch.long)
            for r, (s, _) in enumerate(chunk):
                ids[r, :len(s)] = torch.tensor(s)
                mask[r, :len(s)] = 1
            ids, mask = ids.to(self.device), mask.to(self.device)
            with torch.no_grad():
                logp = torch.log_softmax(self.model(input_ids=ids, attention_mask=mask).logits.float(), dim=-1)
            for r, (s, n_p) in enumerate(chunk):
                # 位置 t の予測は t+1 番目のトークン。続き（n_p 番目以降）だけを足す
                tgt = torch.tensor(s[n_p:], device=self.device)
                out.append(logp[r, n_p - 1:len(s) - 1].gather(1, tgt[:, None]).sum().item())
        return out


@dataclass
class Scored:
    score: float
    conversion: Conversion
    lm: float
    dict_cost: float
    read_cost: float


def rank(convs: list[Conversion], scorer: ZenzScorer, left_context: str = "",
         w_dict: float = 0.0, w_read: float = 1.0) -> list[Scored]:
    """候補を点数の高い順に並べる。候補の読みが違ってもよい（それぞれの読みをプロンプトに入れて採点する）。"""
    if not convs:
        return []
    prompts = [zenz_prompt(c.reading.zenz_input(), left_context) for c in convs]
    lms = scorer.logprobs(prompts, [scoring_text(c.surface) for c in convs])
    scored = [
        Scored(lm - w_dict * c.cost / MOZC_COST_SCALE - w_read * c.reading.cost, c, lm, c.cost, c.reading.cost)
        for c, lm in zip(convs, lms)
    ]
    return sorted(scored, key=lambda s: -s.score)
