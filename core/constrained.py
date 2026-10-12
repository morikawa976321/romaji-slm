"""工程4（制約付き生成）: zenz に貪欲生成させ、出力が辞書ラティスの経路から外れるトークンを禁止する。

N-best の再順位（core.select）は、辞書の上位 N に正解が入らないと選べない（AJIMEE で oracle@100 91%）。
こちらは zenz 自身の好みで生成させつつ、出力を「ラティスのどれかの経路の表層」に限る（llama.cpp の grammar と同じ考え方）。
照合はバイト単位: zenz はバイト単位 BPE で、1トークンが UTF-8 の1文字の途中で切れることがある。
"""

from .kanakanji import Dictionary, _build_lattice
from .select import scoring_text, zenz_prompt
from .types import Reading

def bytes_to_unicode() -> dict[int, str]:
    """GPT-2 のバイト単位 BPE がトークン文字列に使う「バイト → 表示文字」の対応（GPT-2 の公開実装と同じ表）。

    transformers 5 では tokenization_gpt2 から外れたので、ここに持つ。
    """
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


_BAD_HEAD = frozenset("ァィゥェォャュョッヮーン")

# 状態: (ラティスの節点, そこへ向かう途中の語の残りバイト)。残りが b"" なら節点の上にいる
State = tuple[int, bytes]


class LatticeMatcher:
    def __init__(self, reading: Reading, dic: Dictionary):
        lat, self.final = _build_lattice(reading, dic)
        self.out: dict[int, list[tuple[bytes, int]]] = {}
        for s, e, surf, rd, src in zip(lat.start, lat.end, lat.surface, lat.reading, lat.source):
            if src in ("bos", "eos") or not surf:
                continue
            # 辞書に無いかなの断片は、小書き・長音・ンで始まると語として不自然（「時速4時ュウゴ」のような崩れ）
            if src == "kana" and rd[:1] in _BAD_HEAD:
                continue
            b = scoring_text(surf).encode()
            if b:
                self.out.setdefault(s, []).append((b, e))

    def start(self) -> frozenset[State]:
        return frozenset({(0, b"")})

    def advance(self, states: frozenset[State], data: bytes) -> frozenset[State]:
        cur = set(states)
        for byte in data:
            nxt: set[State] = set()
            for node, rest in cur:
                if rest:
                    if rest[0] == byte:
                        nxt.add((node, rest[1:]))
                    continue
                for b, end in self.out.get(node, ()):
                    if b[0] == byte:
                        nxt.add((end, b[1:]))
            cur = {(n, r) if r else (n, b"") for n, r in nxt}
            if not cur:
                return frozenset()
        return frozenset(cur)

    def accepting(self, states: frozenset[State]) -> bool:
        return (self.final, b"") in states


class ConstrainedZenz:
    def __init__(self, scorer, max_tries: int = 2000):
        """scorer: core.select.ZenzScorer（モデルとトークナイザを使い回す）。"""
        self.s = scorer
        tok = scorer.tok
        dec = {c: b for b, c in bytes_to_unicode().items()}
        self.tok_bytes = [bytes(dec[ch] for ch in t) if t is not None else b""
                          for t in tok.convert_ids_to_tokens(list(range(len(tok))))]
        self.eos = tok.eos_token_id
        self.max_tries = max_tries

    def decode(self, reading: Reading, dic: Dictionary, left_context: str = "", max_new: int | None = None) -> str:
        return self.decode_scored(reading, dic, left_context, max_new)[0]

    def decode_scored(self, reading: Reading, dic: Dictionary, left_context: str = "",
                      max_new: int | None = None) -> tuple[str, float, bool]:
        """(出力, 選んだトークンの対数確率の和（EOS を含む）, ラティスの終点まで届いたか)。

        対数確率は制約をかける前の分布で測る（制約で弾いた分を正規化し直さない）。読みの違う候補どうしを
        比べるとき、zenz がその読みと出力をどれだけ自然と見たかの尺度になる。
        """
        torch, model, tok = self.s._torch, self.s.model, self.s.tok
        m = LatticeMatcher(reading, dic)
        states = m.start()
        ids = tok(zenz_prompt(reading.zenz_input(), left_context), add_special_tokens=False,
                  return_tensors="pt").input_ids.to(self.s.device)
        max_new = max_new or 3 * len(reading.zenz_input()) + 8
        out = bytearray()
        past = None
        step_ids = ids
        logp_sum = 0.0
        finished = False
        for _ in range(max_new):
            with torch.no_grad():
                r = model(input_ids=step_ids, past_key_values=past, use_cache=True)
            past = r.past_key_values
            logits = r.logits[0, -1].float()
            logp = torch.log_softmax(logits, dim=-1)
            order = torch.argsort(logits, descending=True).tolist()
            chosen = None
            for t in order[: self.max_tries]:
                if t == self.eos:
                    if m.accepting(states):
                        chosen = t
                        break
                    continue
                b = self.tok_bytes[t]
                if not b:
                    continue
                ns = m.advance(states, b)
                if ns:
                    chosen, states = t, ns
                    break
            if chosen is None:
                break
            logp_sum += logp[chosen].item()
            if chosen == self.eos:
                finished = True
                break
            out += self.tok_bytes[chosen]
            step_ids = torch.tensor([[chosen]], device=self.s.device)
        return out.decode("utf-8", errors="replace"), logp_sum, finished
