"""変換コアの全体: 打鍵列 → 読みの候補（core.reading）→ 辞書ラティスで制約した zenz の生成（core.constrained）→ 1候補。

読みの上位 k 件それぞれについて制約付き生成を行い、

    score = zenz の対数確率（出力 + EOS）+ w_flu × 完成文の自然さ − w_read × 読みのコスト

が最大のものを返す。英語か日本語か・打鍵ミスを直すかは、ここで zenz の尤度と読みのコストの比較で決まる。
"""

import time
from dataclasses import replace

from eval.converters import Result

from .constrained import ConstrainedZenz
from .kanakanji import Dictionary
from .reading import readings, verbatim_spans
from .select import ZenzScorer, fluency
from .types import Edit

FIX_COST = 0.5  # ユーザー辞書の打ち間違いの癖で直した読みに足すコスト（1 編集ぶんより安い。直すかは採点が決める）


_TYPED_PUNCT = frozenset(",.?!")  # 打鍵列に句読点として打たれうる記号
_PUNCT = frozenset("、。？！?!,.")


def punct_variants(text: str, bounds: tuple[int, ...]) -> list[tuple[str, int, str, int | None]]:
    """句読点 B の候補: (文, 読点を足した数, 足した文末の記号 "" / "。" / "？", 読点を入れた文字位置)。無挿入を必ず含む。

    読点は語の切れ目（制約付き生成が通ったラティスの節点）に1つまで、文末は末尾に句読点が無いときだけ。
    """
    if not text:
        return [(text, 0, "", None)]
    ends = ["", "。", "？"] if text[-1] not in _PUNCT else [""]
    commas: list[int | None] = [None] + [b for b in bounds if 0 < b < len(text)
                                         and text[b - 1] not in _PUNCT and text[b] not in _PUNCT]
    out = []
    for e in ends:
        for b in commas:
            t = text if b is None else text[:b] + "、" + text[b:]
            out.append((t + e, int(b is not None), e, b))
    return out


def apply_fix(romaji: str, typed: str, intended: str) -> tuple[str, int] | None:
    """ユーザー辞書の fix を当てた打鍵列と、最初に当てた位置。当たらなければ None。
    大文字で打った区間（そのまま残す区間）に重なる一致は置き換えない。fix は小文字だが、数字・記号を含むと
    区間に重なりうる（4a → ta を GPT-4aru に当てると GPT-4 が壊れる）。"""
    if not typed:
        return None
    spans = verbatim_spans(romaji)
    out, pos, first = [], 0, -1
    i = romaji.find(typed)
    while i >= 0:
        j = i + len(typed)
        if any(i < b and a < j for a, b in spans):
            i = romaji.find(typed, i + 1)
            continue
        out += [romaji[pos:i], intended]
        pos, first = j, first if first >= 0 else i
        i = romaji.find(typed, j)
    if first < 0:
        return None
    return "".join(out) + romaji[pos:], first


_AUTO_UPPER_MAX = 4  # en_case=auto: 辞書に無いこの長さ以下の英字列は頭字語とみなして大文字にする（jr→JR、scc→SCC）


def en_surface(seg, en_case: str) -> str:
    """英語区間1つの表記。Seg.surfaces は（登録表記, 綴り）か（綴り,）。"""
    if en_case in ("typed", "zenz"):
        return seg.text
    registered = seg.surfaces[0] if seg.surfaces else seg.text
    if en_case == "auto" and registered == seg.text and seg.text.isalpha() and len(seg.text) <= _AUTO_UPPER_MAX:
        from .english import SHORT_OK, lexicon
        # 読みラティスが英語の弧にしない英字列（辞書に無い、または許可リスト外の2字）は頭字語とみなす。
        # wordfreq は jr のような略語も小文字で持つので「辞書に無い」だけでは足りない
        if lexicon().get(seg.text) is None or not (len(seg.text) >= 3 or seg.text in SHORT_OK):
            return seg.text.upper()
    return registered


def render_en_case(text: str, reading, en_case: str, bounds: tuple[int, ...] = ()) -> str:
    """選んだ後に英字の表記を置き換える（registered / auto）。

    ラティスでは英語区間を打った綴りのまま生成させているので、出力には各 en 区間の綴りが区間の順に現れる。
    順に探して置き換える。zenz に大文字を採点させない（REDESIGN.md「選ばれた弧から表記を描画」。採点させると
    その読みの尤度が下がり、打鍵ミスを直すか・英語かかなかの比較が歪む）。
    """
    if en_case in ("typed", "zenz"):
        return text
    # 辞書語の表層にも ASCII がある（アイフォン→iPhone）ので、語の切れ目（bounds）に揃う出現だけを英語区間とみなす
    ok_start = {0, *bounds}
    ok_end = {len(text), *bounds}
    pos = 0
    out = []
    for seg in reading.segs:
        if seg.kind != "en":
            continue
        i = text.find(seg.text, pos)
        while i >= 0 and bounds and not (i in ok_start and i + len(seg.text) in ok_end):
            i = text.find(seg.text, i + 1)
        if i < 0:
            continue
        out.append(text[pos:i])
        out.append(en_surface(seg, en_case))
        pos = i + len(seg.text)
    out.append(text[pos:])
    return "".join(out)


class CorePipeline:
    # 既定値は user_style・gen_test・AJIMEE で比べて決めた（docs/EVAL.md「完成文の自然さ」。w_read/w_flu の比が効く）
    def __init__(self, model: str = "Miwa-Keita/zenz-v3.1-small", device: str = "cuda", k: int = 5,
                 n_read: int = 20, w_read: float = 4.0, w_flu: float = 1.0, name: str | None = None,
                 batched: bool = True, punct: str = "auto", en_case: str = "typed",
                 tau_comma: float = -0.5, tau_end: float = -6.0, tau_q: float = -5.0, backend=None,
                 preselect: bool = True, n_pre: int = 30, punct_end: bool = False, user_dict=None,
                 max_tries: int = 2000, comma_tail: bool = True, comma_min_len: int = 10):
        """punct / en_case: 記号と英字の表記モード（core.constrained.LatticeMatcher と render_en_case）。
        punct="insert" は句読点の自動挿入（句読点 B）: 打鍵列に句読点が無いとき、完成文の自然さで読点・句点を補う。
        既定は punct="auto"（句読点は自動で足さず、打った句読点の全角・半角だけを zenz が選ぶ。本人の決定、docs/decisions.md D48）と
        en_case="typed"（本人の選択、D14）。ラティスでは insert と auto は同じなので、insert との違いは補うかどうかだけ。
        tau_comma / tau_end / tau_q: 読点・句点・疑問符の挿入1つあたりに自然さから引く閾値（負なら挿入を促す）。
        既定値は user_style の句読点を打たない版で決めた（EVAL.md「句読点 B」。te=-6 で句点の再現率 0.79・適合率 0.98。
        Wikipedia の断片（gen_test omit_punct）では文末に句点を付けすぎるが、本人の用途は完結した文なのでこちらに寄せた）。
        backend: 生成と採点の差し替え（CPU では core.ort_backend.OrtBackend）。decode_batch・fluency・logprobs を持つもの。
        None なら model を transformers で GPU（device は cuda だけ）に読む。
        max_tries: transformers 版の制約付き生成の探索の幅（core.constrained.ConstrainedZenz）。
        """
        self.dic = Dictionary.load()
        # user_dict: 本人のユーザー辞書（core.userdict）。None なら読まない（評価の再現性のため。常駐入力は既定の置き場所を渡す）
        from .userdict import UserDict, load as load_user
        ud = user_dict if isinstance(user_dict, UserDict) else (load_user(user_dict) if user_dict else UserDict())
        from .english import set_user_words
        set_user_words(ud.en)
        self.dic.add_user_words(ud.words)
        self.fixes = ud.fixes
        self.user_dict = ud
        self.backend = backend
        if backend is None:
            self.scorer = ZenzScorer(model, device)
            self.cz = ConstrainedZenz(self.scorer, max_tries=max_tries)
        self.k, self.n_read, self.w_read, self.w_flu = k, n_read, w_read, w_flu
        self.batched = batched  # k 件の制約付き生成を1バッチで回す（False は1件ずつ。速度の比較用）
        self.punct, self.en_case = punct, en_case
        self.tau_comma, self.tau_end, self.tau_q = tau_comma, tau_end, tau_q
        # punct_end: 句読点 B で文末の「。」「？」も補うか。既定は補わない（本人の決定 2026-10-17「Aを採用する」:
        # 常駐入力では文節ごとに確定することが多く、文の途中に句点が付いた。句点は本人が「.」を打つ）
        self.punct_end = punct_end
        # comma_tail: 文末を補わないときも、読点の位置は文末に「。」を仮に付けた文で比べる（出力には付けない）。
        # tau_comma は文末に「。」が付いた文で決めた値（D8）なので、同じ形で比べる（効きは小さい。EVAL.md「句読点 B」）
        # comma_min_len: 句読点を除いて この字数に満たない文には読点を足さない。常駐入力は文節ごとに短く確定し
        # （利用履歴の 62 確定で中央 4 字、87% が 10 字未満）、短い確定の中の読点は誤りになりやすい。
        # 10 字未満で正解に読点がある文は、評価セット（user_style・synth_user）で 2% ほど
        self.comma_tail, self.comma_min_len = comma_tail, comma_min_len
        # preselect: 読み段の候補 n_pre 件を辞書の変換コストで並べ直して上位 k 件を選ぶ（core.preselect、PR #11）
        self.preselect, self.n_pre = preselect, n_pre
        self.name = name or f"core(k={k},w_read={w_read})"
        if backend is not None:
            self.n_params = backend.n_params
        else:
            self.n_params = sum(p.numel() for p in self.scorer.model.parameters())

    def set_user_dict(self, ud) -> None:
        """本人のユーザー辞書を入れ替える（常駐の変換サーバは、ファイルが更新されたら読み直して呼ぶ）。"""
        from .english import set_user_words
        set_user_words(ud.en)
        self.dic.add_user_words(ud.words)
        self.fixes = ud.fixes
        self.user_dict = ud

    def candidates(self, romaji: str, left_context: str = "") -> list[dict]:
        """読みの上位 k 件の変換と点数（点数の高い順）。"""
        out = []
        if self.preselect:
            from .preselect import preselect as _preselect
            allr = readings(romaji, n=self.n_pre)
            rs = _preselect(allr, self.dic, self.k)
        else:
            allr = readings(romaji, n=self.n_read)
            rs = allr[: self.k]
        # 読み段は上位 k 件を多様性で選び直して返す（1 位はコスト最小、2〜k 位は選び直し。docs/READING.md）。
        # 選び直しは英単語として読む候補（code、main をそのまま残す読み）を押し出すことがあり、user_style で
        # 正常入力が 2 件壊れた。コスト順の上位 k 件も足して比べる（CPU ではバッチが大きくなってもほぼ同じ時間。D3）
        seen = {r.segs for r in rs}
        for r in sorted(allr, key=lambda r: r.cost)[: self.k]:
            if r.segs not in seen:
                rs.append(r)
                seen.add(r.segs)
        # 本人の打ち間違いの癖（ユーザー辞書の fix）: 打鍵列に含まれていれば、直した打鍵列の読みも比べる
        for typed, intended in self.fixes:
            hit = apply_fix(romaji, typed, intended)
            if hit:
                fixed, at = hit
                for r in readings(fixed, n=2):
                    if r.segs not in seen:
                        rs.append(replace(r, edits=r.edits + (Edit("fix", at, f"{typed}>{intended}"),),
                                          cost=r.cost + FIX_COST))
                        seen.add(r.segs)
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
                "bounds": d.bounds, "reading_obj": r,
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

    def insert_punct(self, text: str, bounds: tuple[int, ...], left_context: str = "") -> tuple[str, tuple[int, ...]]:
        """句読点 B: punct_variants の候補を完成文の自然さで比べ、閾値を引いて最良のものを返す。

        戻り値は (文, 挿入後の文字位置に直した語の切れ目)。切れ目は render_en_case が使う。
        """
        vs = punct_variants(text, bounds)
        if not self.punct_end:
            vs = [v for v in vs if v[2] == ""]
        if sum(ch not in _PUNCT for ch in text) < self.comma_min_len:
            vs = [v for v in vs if v[3] is None]
        if len(vs) == 1:
            return text, bounds
        tau_end = {"": 0.0, "。": self.tau_end, "？": self.tau_q}

        def pick(cands, tail=""):
            fl = self._fluency([v[0] + tail for v in cands], left_context)
            return max(zip(cands, fl), key=lambda vf: vf[1] - self.tau_comma * vf[0][1] - tau_end[vf[0][2]])[0]

        # 2 段で選ぶ（まず文末の 3 候補、次にその文末で読点の位置）。全組合せ（3 × 切れ目の数）を1バッチで採点すると
        # CPU で 1 件 300 ms ほど増えた（user_style_nopunct で中央値 379 → 703 ms）。段階に分けると採点する文が約 1/3 になる
        end = pick([v for v in vs if v[3] is None])[2] if self.punct_end else ""
        tail = "。" if self.comma_tail and not self.punct_end and text[-1] not in _PUNCT else ""
        new_text, _, _, b = pick([v for v in vs if v[2] == end], tail)
        if b is not None:
            bounds = tuple(sorted({x if x < b else x + 1 for x in bounds} | {b, b + 1}))
        return new_text, bounds

    def _render_best(self, best: dict, romaji: str, left_context: str) -> str:
        text, bounds = best["text"], best["bounds"]
        if self.punct == "insert" and not (_TYPED_PUNCT & set(romaji)) and best["score"] != float("-inf"):
            # 句読点を打った入力には足さない（打った句読点の種類と幅だけ決める = 句読点 A と同じ振る舞い）
            text, bounds = self.insert_punct(text, bounds, left_context)
        return render_en_case(text, best["reading_obj"], self.en_case, bounds)

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result:
        cands = self.candidates(romaji, left_context)
        if not cands:
            return Result("", prompt=romaji)
        best = cands[0]
        text = self._render_best(best, romaji, left_context)
        return Result(text, prompt=best["reading"], candidates=tuple(c["text"] for c in cands),
                      edited=bool(best["edits"]))

    def convert_nbest(self, romaji: str, left_context: str = "", n: int = 6, order: str | None = None,
                      edit_slots: int | None = None) -> list[str]:
        """常駐の入力（ime/）が次候補に使う、重複のない候補の列。1 位は convert と同じ文。並べ方は core.window。"""
        from . import window
        return window.nbest(self, romaji, left_context, n, order or window.DEFAULT_ORDER, edit_slots)

    def convert(self, romaji: str, left_context: str = "") -> str:
        return self.convert_detailed(romaji, left_context).text
