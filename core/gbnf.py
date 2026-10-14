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
    back: dict[int, set[int]] = {}
    for s, arcs in m.out.items():
        for _, e in arcs:
            back.setdefault(e, set()).add(s)
    live, stack = {m.final}, [m.final]
    while stack:
        for s in back.get(stack.pop(), ()):
            if s not in live:
                live.add(s)
                stack.append(s)
    return live


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


