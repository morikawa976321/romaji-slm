"""辞書ラティス（core.constrained.LatticeMatcher）を llama.cpp の GBNF 文法にする。

ConstrainedZenz は Python でトークンごとにラティスを照合する。llama-server では同じ制約を文法で渡し、
照合は llama.cpp 側に任せる（core.llama_backend）。文法は右線形（節点1つ = 規則1つ）:

    root ::= n0
    n0 ::= "今日" n3 | "京" n3 | "キョウ" n3 | ...
    n3 ::= "は" n4 | ...
    n9 ::= "ね"            # 終点へ入る弧は継続なし

終点に届かない節点（行き止まり）は刈る。文法の受理する文字列の集合は、ラティスの経路の表層の集合と同じ。
"""

from .constrained import LatticeMatcher
from .kanakanji import Dictionary
from .types import Reading


def gbnf_literal(b: bytes) -> str:
    """GBNF の文字列リテラル。LatticeMatcher.out のバイト列は常に完全な UTF-8 文字列（表層を encode したもの）。"""
    out = []
    for ch in b.decode("utf-8"):
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _live_nodes(m: LatticeMatcher) -> set[int]:
    """終点へ届く節点（後ろ向きの到達可能性）。"""
    return m.live_nodes()


def matcher_grammar(m: LatticeMatcher) -> str | None:
    """LatticeMatcher の文法。経路が1つも無ければ None（空の言語は GBNF で書けない）。"""
    live = _live_nodes(m)
    if 0 not in live:
        return None
    if m.final == 0:
        return 'root ::= ""\n'
    # 終点以外で出ていく生きた弧を持つ節点だけが規則になる
    rules: dict[int, list[str]] = {}
    for s in sorted(m.out):
        if s not in live:
            continue
        alts: list[str] = []
        seen: set[str] = set()
        for b, e in m.out[s]:
            if e not in live:
                continue
            lit = gbnf_literal(b)
            cands = []
            if e == m.final:
                cands.append(lit)
            if e != m.final or any(ee in live for _, ee in m.out.get(e, ())):
                cands.append(f"{lit} n{e}")
            for a in cands:
                if a not in seen:
                    seen.add(a)
                    alts.append(a)
        if alts:
            rules[s] = alts
    lines = ["root ::= n0"] + [f"n{s} ::= " + " | ".join(alts) for s, alts in rules.items()]
    return "\n".join(lines) + "\n"


DEAD_END = '"\\x7F"'  # 行き止まりの印（DEL。辞書の表層には現れない。zenz の語彙では1バイトの1トークン）


def matcher_grammar_stop(m: LatticeMatcher, sentinel: str = DEAD_END) -> str:
    """行き止まりを刈らない文法（torch 版 ConstrainedZenz の振る舞いを再現する変種）。

    出ていく弧の無い節点（終点を除く）は印 sentinel だけを許す。印は logit_bias で最後の手段にしておき
    （core.llama_backend）、出力に印が出たら「候補が尽きて止まった」= 未完とみなす。
    """
    if m.final == 0:
        return 'root ::= ""\n'
    reach, stack = {0}, [0]
    while stack:
        for _, e in m.out.get(stack.pop(), ()):
            if e not in reach:
                reach.add(e)
                stack.append(e)
    final_cont = bool(m.out.get(m.final))
    rules: dict[int, list[str]] = {}
    for s in sorted(reach):
        if s == m.final and not final_cont:
            continue
        alts: list[str] = []
        for b, e in m.out.get(s, ()):
            lit = gbnf_literal(b)
            for a in ([lit] if e == m.final else []) + ([f"{lit} n{e}"] if e != m.final or final_cont else []):
                if a not in alts:
                    alts.append(a)
        rules[s] = alts or [sentinel]
    lines = ["root ::= n0"] + [f"n{s} ::= " + " | ".join(alts) for s, alts in rules.items()]
    return "\n".join(lines) + "\n"


def lattice_grammar(reading: Reading, dic: Dictionary, punct: str = "auto", en_case: str = "zenz") -> str | None:
    return matcher_grammar(LatticeMatcher(reading, dic, punct, en_case))


def grammar_size(grammar: str | None) -> tuple[int, int]:
    """(規則の数, UTF-8 のバイト数)。速度の記録用。"""
    if not grammar:
        return 0, 0
    return grammar.count("\n"), len(grammar.encode())


def bounds_from_text(m: LatticeMatcher, text: str) -> tuple[int, ...]:
    """出力を辿り直して語の切れ目を得る。実装は core.constrained.bounds_from_text（バイトごとに節点を見るので、
    複数文字トークンの内部の切れ目も Decoded.bounds と同じに取れる）。"""
    from .constrained import bounds_from_text as _bounds
    return _bounds(m, text)


