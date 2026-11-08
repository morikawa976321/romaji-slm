"""工程4（制約付き生成）: zenz に貪欲生成させ、出力が辞書ラティスの経路から外れるトークンを禁止する。

N-best の再順位（core.select）は、辞書の上位 N に正解が入らないと選べない（AJIMEE で oracle@100 91%）。
こちらは zenz 自身の好みで生成させつつ、出力を「ラティスのどれかの経路の表層」に限る。
照合はバイト単位: zenz はバイト単位 BPE で、1トークンが UTF-8 の1文字の途中で切れることがある。
"""

import re
from typing import NamedTuple

from .cancel import check as _check_cancel
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
_KANA_ONLY = re.compile(r"^[ぁ-ゖァ-ヺー]+$")

# 状態: (ラティスの節点, そこへ向かう途中の語の残りバイト)。残りが b"" なら節点の上にいる
State = tuple[int, bytes]


PUNCT_MODES = ("auto", "ja", "ascii", "insert")  # insert はラティスでは auto と同じ（挿入は core.pipeline が後段で行う）
EN_CASE_MODES = ("zenz", "typed", "registered", "auto")


def _en_surface(seg, en_case: str) -> str:
    """ラティスに置く英語区間の表層。zenz 以外のモードでは打った綴りだけにする。

    登録表記（GitHub）を zenz に採点させると、大文字の分だけその読みの尤度が下がり、読みの比較（打鍵ミスを
    直すか・英語かかなか）が歪む（user_style で誤補正 4.8%→9.7%、typo 63.6%→54.5%）。表記は選んだ後に
    core.pipeline.render_en_case が置き換える（REDESIGN.md「選ばれた弧から表記を描画。zenz には出させない」）。
    """
    return seg.text


class LatticeMatcher:
    def __init__(self, reading: Reading, dic: Dictionary, punct: str = "auto", en_case: str = "zenz", vocab=None):
        """punct: 記号の表記。auto=全角/半角の両方を候補に残して zenz に選ばせる、ja=全角だけ、ascii=打ったまま。
        en_case: 英字の表記。zenz=登録表記と綴りの両方を残して zenz に選ばせる（旧来の振る舞い）。それ以外
        （typed / registered / auto）はラティスでは打った綴りだけにし、表記の置き換えは後段（core.pipeline.render_en_case）。
        行き止まり（終点に届かない節点）は刈らない。貪欲法が行き止まりに入った読みは未完（score −∞）で捨てられる。
        vocab: 本人の語彙の点（core.uservocab.UserVocab。同じ語の本人が使わない書き方ほど負）。辞書の語とかなの弧に付け、
        advance_acc で持ち回る。
        """
        if punct not in PUNCT_MODES:
            raise ValueError(f"punct は {PUNCT_MODES} のどれか: {punct}")
        if en_case not in EN_CASE_MODES:
            raise ValueError(f"en_case は {EN_CASE_MODES} のどれか: {en_case}")
        lat, self.final = _build_lattice(reading, dic)
        self.out: dict[int, list[tuple[bytes, int]]] = {}
        self.bonus: dict[tuple[int, bytes, int], float] = {}  # (始点, 表層, 終点) → その語に入ったときの点（0 の弧は持たない）
        seen: set[tuple[int, bytes, int]] = set()
        for s, e, surf, rd, si, src in zip(lat.start, lat.end, lat.surface, lat.reading, lat.seg, lat.source):
            if src in ("bos", "eos") or not surf:
                continue
            # 辞書に無いかなの断片は、小書き・長音・ンで始まると語として不自然（「時速4時ュウゴ」のような崩れ）
            if src == "kana" and rd[:1] in _BAD_HEAD:
                continue
            if src == "sym" and punct in ("ja", "ascii") and surf not in _sym_surfaces(reading.segs[si], punct):
                continue
            if src == "en" and en_case != "zenz":
                seg = reading.segs[si]
                surf = _en_surface(seg, en_case)
            b = scoring_text(surf).encode()
            if b and (s, b, e) not in seen:
                seen.add((s, b, e))
                self.out.setdefault(s, []).append((b, e))
            if b and vocab and src in ("dict", "kana"):
                v = vocab.get(rd, surf)
                self.bonus[(s, b, e)] = max(self.bonus.get((s, b, e), v), v)
        self.bonus = {k: v for k, v in self.bonus.items() if v}
        # 経路の点は最大を取るので、減点のある語（欲しい）と同じ文字列を短い語の連なり（欲し＋い）で出すと減点が消える。
        # 同じ位置から始まり、表記も読みもその語の頭になっている短い語にも同じ減点を付けてふさぐ。その語より長い語
        # （欲しがる）の弧は 0 のままなので、長い語として出す道は減らない。かなだけの頭（はいる の は・はい）には付けない —
        # 助詞などの別の語としての区切りと見分けられず、助詞の は＋いる を 入る に変えた（2026-11-08）
        for (s, b, e), v in [kv for kv in self.bonus.items() if kv[1] < 0]:
            for b2, e2 in self.out.get(s, ()):
                if e2 < e and len(b2) < len(b) and b.startswith(b2) and not _KANA_ONLY.match(b2.decode()):
                    self.bonus[(s, b2, e2)] = min(self.bonus.get((s, b2, e2), 0.0), v)
        self.bonus_max = max(0.0, *self.bonus.values()) if self.bonus else 0.0  # 1 語で入る点の最大（pick_with_bonus の打ち切り）

    def start(self) -> frozenset[State]:
        return frozenset({(0, b"")})

    def start_acc(self) -> dict[State, float]:
        return {(0, b""): 0.0}

    def advance_acc(self, acc: dict[State, float], data: bytes) -> dict[State, float]:
        """advance と同じ進め方で、状態ごとに「そこまでの経路で入った語の加点の和」の最大を持ち回る（Viterbi）。
        語の加点は、その語の最初のバイトを読んだ時点で足す（貪欲法がその語に入るかどうかを決める位置）。"""
        cur = acc
        bonus, out = self.bonus, self.out
        for byte in data:
            nxt: dict[State, float] = {}
            for (node, rest), v in cur.items():
                if rest:
                    if rest[0] == byte:
                        k = (node, rest[1:])
                        if v > nxt.get(k, -1e18):
                            nxt[k] = v
                    continue
                for b, end in out.get(node, ()):
                    if b[0] == byte:
                        k = (end, b[1:])
                        w = v + bonus.get((node, b, end), 0.0)
                        if w > nxt.get(k, -1e18):
                            nxt[k] = w
            if not nxt:
                return {}
            cur = nxt
        return cur

    def bonus_of(self, text: str) -> float:
        """文全体の語の加点の和（その文を出せる経路のうち最大）。出せない文は 0。生成していない文（core.window）用。"""
        if not self.bonus:
            return 0.0
        acc = self.advance_acc(self.start_acc(), scoring_text(text).encode())
        return acc.get((self.final, b""), 0.0)

    def advance(self, states: frozenset[State], data: bytes) -> frozenset[State]:
        return self.advance_bounds(states, data)[0]

    def advance_bounds(self, states: frozenset[State], data: bytes) -> tuple[frozenset[State], list[int]]:
        """data を1バイトずつ進め、(進めた状態, 節点の上に状態があったバイト位置（1 始まり）) を返す。

        語の切れ目はトークンの途中にもある（複数文字のトークンで「今日は」を1つで出すときの「今日|は」）ので、
        トークンの末尾だけでなくバイトごとに記録する。
        """
        cur = set(states)
        bounds: list[int] = []
        for k, byte in enumerate(data, 1):
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
                return frozenset(), []
            if any(not rest for _, rest in cur):
                bounds.append(k)
        return frozenset(cur), bounds

    def accepting(self, states: frozenset[State]) -> bool:
        return (self.final, b"") in states


def _sym_surfaces(seg, punct: str) -> frozenset[str]:
    """記号区間で許す表層。ja なら全角、ascii なら打ったまま。該当が無い記号（@ + & % など全角候補の無いもの）は
    全部許す（絞ると行き止まりになり、変換が完走しない）。"""
    surfaces = seg.surfaces or (seg.text,)
    want = [s for s in surfaces if (punct == "ja") != s.isascii()]
    return frozenset(want or surfaces)


def bounds_from_text(matcher: LatticeMatcher, text: str) -> tuple[int, ...]:
    """出力の文字列をラティスで先頭から辿り直し、語の切れ目になりうる文字位置を返す（Decoded.bounds と同じ定義）。
    生成していない文（候補ウィンドウの別の書き方。core.window）のために使う。"""
    data = scoring_text(text).encode()
    states, bounds = matcher.advance_bounds(matcher.start(), data)
    return _char_bounds(data, bounds)


class Decoded(NamedTuple):
    text: str
    logp: float  # 選んだトークンの対数確率の和（EOS を含む。制約をかける前の分布で測る）
    finished: bool  # ラティスの終点まで届いて EOS を選べたか
    bounds: tuple[int, ...] = ()  # 語の切れ目になりうる文字位置（0 と末尾を除く。句読点の自動挿入の候補位置）
    bonus: float = 0.0  # 本人の語彙の点の和（重みを掛ける前。≤ 0。LatticeMatcher.advance_acc。語彙を使わなければ 0）


# 1 トークンで入る語の数の見込みの上限。logit が これ × 重み × 1 語の点の最大 より低いトークンは、点で逆転しえないので
# 比べない（複数文字のトークンが正の点の語を 3 つ以上またぐと見落とすが、選びが少し本人寄りでなくなるだけ）。
# 本人の語彙の点は ≤ 0（core.uservocab）なので、ふだんは最初の合法トークンより logit の低いものだけを比べる
BONUS_SPAN = 2.0


def pick_with_bonus(order, m: LatticeMatcher, acc: dict[State, float], w: float, tok_bytes, eos: int):
    """order: (トークン, logit) を logit の降順に。制約を満たすトークンのうち、logit + w ×（経路の加点の最大の増分）が
    最大のものと、進めた状態（加点つき）を返す。無ければ (None, None)。

    加点が無ければ（w=0）最初に見つかった合法トークン = 加点なしの貪欲法と同じ選びになる。"""
    base = max(acc.values())
    slack = w * BONUS_SPAN * m.bonus_max
    best = None
    for t, lg in order:
        if best is not None and lg + slack < best[0]:
            break
        if t == eos:
            v = acc.get((m.final, b""))
            if v is None:
                continue
            cand = (lg + w * (v - base), t, acc)
        else:
            b = tok_bytes[t]
            if not b:
                continue
            na = m.advance_acc(acc, b)
            if not na:
                continue
            cand = (lg + w * (max(na.values()) - base), t, na)
        if best is None or cand[0] > best[0]:
            best = cand
        if w <= 0:
            break
    return (best[1], best[2]) if best else (None, None)


def final_bonus(m: LatticeMatcher, acc: dict[State, float] | None, finished: bool) -> float:
    """生成し終えた文の語彙の加点の和（終点の状態の値）。加点を使っていない・終点まで届かなかったなら 0。"""
    return acc.get((m.final, b""), 0.0) if finished and acc else 0.0


def _char_bounds(out: bytes, byte_offsets: list[int]) -> tuple[int, ...]:
    n = len(out)
    return tuple(sorted({len(out[:o].decode("utf-8", errors="ignore")) for o in byte_offsets if 0 < o < n}))


class ConstrainedZenz:
    def __init__(self, scorer, max_tries: int = 2000):
        """scorer: core.select.ZenzScorer（モデルとトークナイザを使い回す）。
        max_tries: 制約を満たすトークンを確率の上位何位まで探すか（0 なら語彙全体）。"""
        self.s = scorer
        tok = scorer.tok
        dec = {c: b for b, c in bytes_to_unicode().items()}
        self.tok_bytes = [bytes(dec[ch] for ch in t) if t is not None else b""
                          for t in tok.convert_ids_to_tokens(list(range(len(tok))))]
        self.eos = tok.eos_token_id
        self.max_tries = max_tries or len(self.tok_bytes)

    def decode(self, reading: Reading, dic: Dictionary, left_context: str = "", max_new: int | None = None) -> str:
        return self.decode_scored(reading, dic, left_context, max_new)[0]

    def decode_scored(self, reading: Reading, dic: Dictionary, left_context: str = "",
                      max_new: int | None = None, punct: str = "auto", en_case: str = "zenz",
                      vocab=None, w_voc: float = 0.0) -> Decoded:
        """1件ずつの制約付き生成（Decoded: 出力・対数確率の和・終点まで届いたか・語の切れ目・語彙の加点）。

        対数確率は制約をかける前の分布で測る（制約で弾いた分を正規化し直さない）。読みの違う候補どうしを
        比べるとき、zenz がその読みと出力をどれだけ自然と見たかの尺度になる。
        vocab と w_voc > 0 を渡すと、トークンを logit + w_voc ×（本人の語彙の加点の増分）で選ぶ（pick_with_bonus）。
        """
        torch, model, tok = self.s._torch, self.s.model, self.s.tok
        m = LatticeMatcher(reading, dic, punct, en_case, vocab)
        states = m.start()
        acc = m.start_acc() if w_voc > 0 and m.bonus else None
        ids = tok(zenz_prompt(reading.zenz_input(), left_context), add_special_tokens=False,
                  return_tensors="pt").input_ids.to(self.s.device)
        max_new = max_new or 3 * len(reading.zenz_input()) + 8
        out = bytearray()
        past = None
        step_ids = ids
        logp_sum = 0.0
        finished = False
        bounds: list[int] = []
        for _ in range(max_new):
            _check_cancel()
            with torch.no_grad():
                r = model(input_ids=step_ids, past_key_values=past, use_cache=True)
            past = r.past_key_values
            logits = r.logits[0, -1].float()
            prev_states = states
            if acc is None:
                chosen, states = self._pick(logits, m, states)
            else:
                chosen, acc = self._pick_acc(logits, m, acc, w_voc)
                states = frozenset(acc) if acc else None
            if chosen is None:
                break
            logp_sum += torch.log_softmax(logits, dim=-1)[chosen].item()
            if chosen == self.eos:
                finished = True
                break
            base = len(out)
            out += self.tok_bytes[chosen]
            bounds.extend(base + k for k in m.advance_bounds(prev_states, self.tok_bytes[chosen])[1])
            step_ids = torch.tensor([[chosen]], device=self.s.device)
        return Decoded(out.decode("utf-8", errors="replace"), logp_sum, finished, _char_bounds(bytes(out), bounds),
                       final_bonus(m, acc, finished))

    def decode_batch(self, readings: list[Reading], dic: Dictionary, left_context: str = "",
                     max_new: int | None = None, punct: str = "auto", en_case: str = "zenz",
                     vocab=None, w_voc: float = 0.0) -> list[Decoded]:
        """k 件の読みを1つのバッチで制約付き生成する。結果は decode_scored を1件ずつ呼んだのと同じ。

        CPU では forward 1回の固定費が支配的で、バッチを 5 にしても 1 件の約 1.4 倍の時間で済む
        （docs/decisions.md D3）。左パディングするので attention_mask に加えて position_ids を系列ごとに渡す
        （GPT-2 は絶対位置埋め込み。渡さないとパディングの分だけ位置がずれ、1件ずつの結果と変わる）。
        """
        if not readings:
            return []
        torch, model, tok = self.s._torch, self.s.model, self.s.tok
        dev = self.s.device
        matchers = [LatticeMatcher(r, dic, punct, en_case, vocab) for r in readings]
        states = [m.start() for m in matchers]
        accs = [m.start_acc() if w_voc > 0 and m.bonus else None for m in matchers]
        prompts = [tok(zenz_prompt(r.zenz_input(), left_context), add_special_tokens=False).input_ids
                   for r in readings]
        B, L = len(prompts), max(len(p) for p in prompts)
        pad = self.s.pad
        ids = torch.full((B, L), pad, dtype=torch.long)
        mask = torch.zeros((B, L), dtype=torch.long)
        for i, p in enumerate(prompts):
            ids[i, L - len(p):] = torch.tensor(p)
            mask[i, L - len(p):] = 1
        pos = (mask.cumsum(-1) - 1).clamp(min=0)
        ids, mask, pos = ids.to(dev), mask.to(dev), pos.to(dev)
        limits = [max_new or 3 * len(r.zenz_input()) + 8 for r in readings]
        outs = [bytearray() for _ in readings]
        logp_sum = [0.0] * B
        finished = [False] * B
        alive = [True] * B
        bounds: list[list[int]] = [[] for _ in readings]
        step_ids, step_pos, past = ids, pos, None
        for step in range(max(limits)):
            _check_cancel()
            with torch.no_grad():
                r = model(input_ids=step_ids, attention_mask=mask, position_ids=step_pos,
                          past_key_values=past, use_cache=True)
            past = r.past_key_values
            logits = r.logits[:, -1].float()
            nxt = [pad] * B
            for i in range(B):
                if not alive[i]:
                    continue
                if step >= limits[i]:
                    alive[i] = False
                    continue
                if accs[i] is None:
                    chosen, ns = self._pick(logits[i], matchers[i], states[i])
                else:
                    chosen, na = self._pick_acc(logits[i], matchers[i], accs[i], w_voc)
                    if chosen is not None:
                        accs[i], ns = na, frozenset(na)
                if chosen is None:
                    alive[i] = False
                    continue
                logp_sum[i] += torch.log_softmax(logits[i], dim=-1)[chosen].item()
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
            # 終わった系列にはパディングを食わせ続ける（結果は使わない。他の系列には影響しない）。位置は進めない
            # （進め続けると、長いプロンプトの系列が先に終わったとき位置埋め込みの上限 n_positions を超えうる）
            step_ids = torch.tensor(nxt, device=dev).unsqueeze(1)
            mask = torch.cat([mask, torch.ones((B, 1), dtype=torch.long, device=dev)], dim=1)
            step_pos = step_pos[:, -1:] + torch.tensor([[int(a)] for a in alive], device=dev)
        return [Decoded(o.decode("utf-8", errors="replace"), lp, f, _char_bounds(bytes(o), bd), final_bonus(m, a, f))
                for o, lp, f, bd, m, a in zip(outs, logp_sum, finished, bounds, matchers, accs)]

    _TOP = 64  # ほとんどの位置はこの中で決まる。足りなければ max_tries まで全順位を見る

    def _pick_acc(self, logits, m: LatticeMatcher, acc: dict[State, float], w: float):
        """_pick の語彙の加点つき（pick_with_bonus）。"""
        torch = self.s._torch
        n_top = min(self._TOP, self.max_tries, logits.shape[-1])
        vals, idx = torch.topk(logits, n_top)
        t, na = pick_with_bonus(zip(idx.tolist(), vals.tolist()), m, acc, w, self.tok_bytes, self.eos)
        if t is not None or self.max_tries <= n_top:
            return t, na
        order = torch.argsort(logits, descending=True)[n_top:self.max_tries]
        return pick_with_bonus(zip(order.tolist(), logits[order].tolist()), m, acc, w, self.tok_bytes, self.eos)

    def _pick(self, logits, m: LatticeMatcher, states: frozenset[State]):
        """制約を満たす最も確率の高いトークンと、進めた状態。無ければ (None, None)。"""
        torch = self.s._torch
        n_top = min(self._TOP, self.max_tries, logits.shape[-1])
        top = torch.topk(logits, n_top).indices.tolist()
        t, ns = self._scan(top, m, states)
        if t is not None or self.max_tries <= n_top:
            return t, ns
        order = torch.argsort(logits, descending=True).tolist()[n_top:self.max_tries]
        return self._scan(order, m, states)

    def _scan(self, order, m: LatticeMatcher, states: frozenset[State]):
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
