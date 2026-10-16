"""辞書による変換候補（読み → 表記の N-best）。docs/KANAKANJI.md。

    uv run python -m core.kanakanji "キョウハイイテンキデスネ" [-n 20]

Reading（core/types.py）の区間をつないで1本のラティスを作り、Mozc の接続コストで Viterbi（前向き）→
A*（後ろ向き）で N-best を得る。kana 区間は辞書の語の弧と、かなのままの弧（行き止まりを作らないため）。
en・num・sym 区間は表記の候補ごとに1語の弧。
"""

import argparse
import heapq
import struct
import sys
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

from .numbers import DATE_READINGS, number_prefixes
from .numbers import surfaces as num_surfaces
from .types import Conversion, Reading, Seg, Word

ROOT = Path(__file__).resolve().parent.parent
MOZC_DIR = ROOT / "models" / "mozc"

try:
    import marisa_trie
except ImportError:  # 依存グループ core を入れていない環境（テストの fixture 辞書だけなら無くてよい）
    marisa_trie = None

_ENTRY = struct.Struct("<HHH")  # lid, rid, cost。続けて表層の UTF-8
_INF = float("inf")
_CACHE_FORMAT = "1"


def hira_to_kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _normalize_voiced(s: str) -> str:
    # Mozc の japanese_util::NormalizeVoicedSoundMark と同じく、独立した濁点・半濁点を直前のかなに合成する
    if "゛" not in s and "゜" not in s:
        return s
    import unicodedata
    return unicodedata.normalize("NFC", s.replace("゛", "゙").replace("゜", "゚"))


class _DictIndex:
    """marisa-trie が無いときの索引: 読み → 値の並び。前方一致は読みの長さを1つずつ伸ばして引く。"""

    def __init__(self, items):
        self._d: dict[str, list[bytes]] = {}
        for k, v in items:
            self._d.setdefault(k, []).append(v)
        self._maxlen = max((len(k) for k in self._d), default=0)

    def prefixes(self, s: str) -> list[str]:
        return [s[:l] for l in range(1, min(len(s), self._maxlen) + 1) if s[:l] in self._d]

    def __getitem__(self, k: str) -> list[bytes]:
        return self._d[k]


class _MergedIndex:
    """Mozc の索引に、本人のユーザー辞書の語（core.userdict の word）を重ねた索引。"""

    def __init__(self, base, extra: _DictIndex):
        self.base, self.extra = base, extra

    def prefixes(self, s: str) -> list[str]:
        return list(dict.fromkeys(self.base.prefixes(s) + self.extra.prefixes(s)))

    def __getitem__(self, k: str) -> list[bytes]:
        try:
            vals = list(self.base[k])
        except KeyError:
            vals = []
        vals += self.extra._d.get(k, [])
        if not vals:
            raise KeyError(k)
        return vals


def _build_index(items):
    if marisa_trie is not None:
        return marisa_trie.BytesTrie(items)
    return _DictIndex(items)


@dataclass
class Costs:
    """辞書の外から足す弧のコスト（Mozc の生起コストと同じ尺度。-log(p)*500 相当）。値は docs/KANAKANJI.md で調整した結果。"""

    unk_kata_base: int = 9000  # 辞書に無い区間をカタカナのまま出す弧: base + per_char * 字数
    unk_kata_per_char: int = 2000
    unk_hira_base: int = 9000  # 同じくひらがなのまま
    unk_hira_per_char: int = 3000
    unk_max_len: int = 16  # かなのままの弧の最大字数（1字の弧は常に張るので行き止まりにはならない）
    en: int = 3000  # 英語・数字・記号の弧の生起コスト
    num: int = 3000
    sym: int = 1000
    alt_surface: int = 500  # Seg.surfaces の2番目以降の候補に、順位ごとに足す


@dataclass
class _Lattice:
    start: list
    end: list
    lid: list
    rid: list
    cost: list
    surface: list
    reading: list
    seg: list
    source: list

    def add(self, start, end, lid, rid, cost, surface, reading, seg, source) -> None:
        self.start.append(start)
        self.end.append(end)
        self.lid.append(lid)
        self.rid.append(rid)
        self.cost.append(cost)
        self.surface.append(surface)
        self.reading.append(reading)
        self.seg.append(seg)
        self.source.append(source)


class Dictionary:
    """読み（カタカナ）→ 語の索引、接続コスト表、品詞 ID。"""

    def __init__(self, index, matrix: array, pos_size: int, pos_names: list[str], costs: Costs | None = None):
        self.index = index
        self.matrix = matrix  # matrix[rid * pos_size + lid]（Mozc の connection_single_column.txt と同じ並び）
        self.pos_size = pos_size
        self.pos_names = pos_names
        self.pos_ids = {name: i for i, name in enumerate(pos_names)}
        self.costs = costs or Costs()
        self.noun_id = self._pos("名詞,一般,*,*,*,*,*")
        self.num_id = self._pos("名詞,数,アラビア数字,*,*,*,*", self.noun_id)
        self.unk_id = self.noun_id
        self.en_id = self.noun_id
        self.stats: dict = {}

    def _pos(self, name: str, default: int | None = None) -> int:
        i = self.pos_ids.get(name, default)
        if i is None:
            raise KeyError(f"id.def に品詞がない: {name}")
        return i

    def add_user_words(self, words: list[tuple[str, str, int]]) -> None:
        """本人のユーザー辞書の語 (読み, 表記, コスト) を一般名詞として足す（core.userdict）。読みはひらがな・カタカナどちらでも。"""
        if not words:
            return
        items = [(hira_to_kata(r), _ENTRY.pack(self.noun_id, self.noun_id, c) + s.encode("utf-8")) for r, s, c in words]
        base = self.index.base if isinstance(self.index, _MergedIndex) else self.index
        self.index = _MergedIndex(base, _DictIndex(items))

    def conn(self, rid: int, lid: int) -> int:
        return self.matrix[rid * self.pos_size + lid]

    def lookup(self, reading: str) -> list[tuple[int, int, int, str]]:
        """完全一致: (lid, rid, cost, 表層) の並び。"""
        try:
            vals = self.index[reading]
        except KeyError:
            return []
        return [(*_ENTRY.unpack_from(v), v[_ENTRY.size:].decode("utf-8")) for v in vals]

    def sym_id(self, surface: str) -> int:
        """記号の品詞: id.def に表層つきの行があればそれ、無ければ種類（句点・読点・括弧）の総称、それも無ければ記号,一般。"""
        cat = ("句点" if surface in "。．.!！?？…‥" else "読点" if surface in "、，," else
               "括弧開" if surface in "「『（(［[｛{〈《【" else "括弧閉" if surface in "」』）)］]｝}〉》】" else "一般")
        for name in (f"記号,{cat},*,*,*,*,{surface}", f'記号,{cat},*,*,*,*,"{surface}"',
                     f"記号,{cat},*,*,*,*,*", "記号,一般,*,*,*,*,*"):
            if name in self.pos_ids:
                return self.pos_ids[name]
        return self.noun_id

    # ---- 読み込み ----

    @classmethod
    def from_entries(cls, entries, matrix: list[list[int]], pos_names: list[str], costs: Costs | None = None):
        """小さな辞書（テスト用）: entries は (読み, lid, rid, cost, 表層)。matrix[rid][lid]。読みはひらがな・カタカナどちらでも。"""
        items = [(hira_to_kata(r), _ENTRY.pack(l, ri, c) + s.encode("utf-8")) for r, l, ri, c, s in entries]
        size = len(pos_names)
        flat = array("H", (matrix[r][l] for r in range(size) for l in range(size)))
        return cls(_build_index(items), flat, size, pos_names, costs)

    @classmethod
    def load(cls, path: Path | str = MOZC_DIR, costs: Costs | None = None, cache: bool = True) -> "Dictionary":
        """Mozc の dictionary_oss（core.fetch_mozc で取得）を読む。

        初回は本文から索引を作り、path/compiled/ に保存する（marisa-trie があるとき）。以降はそれを mmap で開く。
        """
        path = Path(path)
        t0 = time.perf_counter()
        pos_names = cls._read_id_def(path / "id.def")
        compiled = path / "compiled"
        version = (path / "VERSION").read_text(encoding="utf-8").strip() if (path / "VERSION").exists() else ""
        stamp = f"{_CACHE_FORMAT} {version}"
        use_cache = cache and marisa_trie is not None
        if use_cache and (compiled / "STAMP").exists() and (compiled / "STAMP").read_text(encoding="utf-8") == stamp:
            index = marisa_trie.BytesTrie()
            index.mmap(str(compiled / "dict.marisa"))
            matrix = array("H")
            with open(compiled / "connection.bin", "rb") as f:
                matrix.frombytes(f.read())
            size = int((compiled / "POS_SIZE").read_text())
            source = "cache"
        else:
            index = _build_index(cls._read_dictionaries(path))
            size, matrix = cls._read_connection(path / "connection_single_column.txt")
            source = "text"
            if use_cache:
                compiled.mkdir(exist_ok=True)
                index.save(str(compiled / "dict.marisa"))
                (compiled / "connection.bin").write_bytes(matrix.tobytes())
                (compiled / "POS_SIZE").write_text(str(size))
                (compiled / "STAMP").write_text(stamp, encoding="utf-8")
        if size != len(pos_names):
            raise ValueError(f"接続表の大きさ {size} と id.def の品詞数 {len(pos_names)} が合わない")
        dic = cls(index, matrix, size, pos_names, costs)
        dic.stats = {"load_sec": time.perf_counter() - t0, "source": source}
        return dic

    @staticmethod
    def _read_id_def(path: Path) -> list[str]:
        names: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            i, name = line.split(" ", 1)
            if int(i) != len(names):
                raise ValueError(f"id.def の ID が連番でない: {line}")
            names.append(name)
        return names

    @staticmethod
    def _read_dictionaries(path: Path):
        # 列: 読み（ひらがな）\t lid \t rid \t cost \t 表層 [\t ラベル]（Mozc src/dictionary/text_dictionary_loader.cc）
        for f in sorted(path.glob("dictionary[0-9][0-9].txt")):
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    cols = [c for c in line.rstrip("\r\n").split("\t") if c]
                    if len(cols) < 5:
                        continue
                    if len(cols) > 5:
                        # SPELLING_CORRECTION は誤った読み（Mozc では訂正の提示用）。ZIP_CODE・ENGLISH は OSS 辞書に無い
                        continue
                    reading = hira_to_kata(_normalize_voiced(cols[0]))
                    yield reading, _ENTRY.pack(int(cols[1]), int(cols[2]), int(cols[3])) + \
                        _normalize_voiced(cols[4]).encode("utf-8")

    @staticmethod
    def _read_connection(path: Path) -> tuple[int, array]:
        # 1行目が品詞数 N、続く N*N 行が cost。i 行目は rid = i // N（左の語）、lid = i % N（右の語）
        # （Mozc src/data_manager/gen_connection_data.py。rid=lid=0 は 0 に読み替える）
        with open(path, encoding="utf-8") as fh:
            size = int(fh.readline())
            matrix = array("H", map(int, fh.read().split()))
        if len(matrix) != size * size:
            raise ValueError(f"接続表の要素数 {len(matrix)} が {size}^2 でない")
        matrix[0] = 0
        return size, matrix


# ---- 変換 ----

def _build_lattice(reading: Reading, dic: Dictionary) -> tuple[_Lattice, int]:
    c = dic.costs
    lat = _Lattice([], [], [], [], [], [], [], [], [])
    lat.add(-1, 0, 0, 0, 0, "", "", -1, "bos")  # BOS/EOS は品詞 0
    pos = 0
    for si, seg in enumerate(reading.segs):
        if seg.kind == "kana":
            text = seg.text
            n = len(text)
            for i in range(n):
                rest = text[i:]
                for key in dic.index.prefixes(rest):
                    for lid, rid, cost, surf in dic.lookup(key):
                        lat.add(pos + i, pos + i + len(key), lid, rid, cost, surf, key, si, "dict")
                # 数の読み → 数字（辞書に合成数がほぼ無いため。core/numbers.py）
                for end, value in number_prefixes(text, i):
                    for k, surf in enumerate(num_surfaces(value)):
                        lat.add(pos + i, pos + end, dic.num_id, dic.num_id, c.num + c.alt_surface * k,
                                surf, text[i:end], si, "num")
                for dr, day in DATE_READINGS.items():
                    if rest.startswith(dr):
                        for k, surf in enumerate(num_surfaces(day)[:2]):
                            lat.add(pos + i, pos + i + len(dr), dic.num_id, dic.num_id, c.num + c.alt_surface * k,
                                    surf + "日", dr, si, "num")
                for l in range(1, min(n - i, c.unk_max_len) + 1):
                    kana = rest[:l]
                    lat.add(pos + i, pos + i + l, dic.unk_id, dic.unk_id,
                            c.unk_kata_base + c.unk_kata_per_char * l, kana, kana, si, "kana")
                    hira = kata_to_hira(kana)
                    if hira != kana:
                        lat.add(pos + i, pos + i + l, dic.unk_id, dic.unk_id,
                                c.unk_hira_base + c.unk_hira_per_char * l, hira, kana, si, "kana")
            pos += n
        else:
            surfaces = seg.surfaces or (seg.text,)
            for k, surf in enumerate(surfaces):
                if seg.kind == "sym":
                    pid, base = dic.sym_id(surf), c.sym
                elif seg.kind == "num":
                    pid, base = dic.num_id, c.num
                else:
                    pid, base = dic.en_id, c.en
                lat.add(pos, pos + 1, pid, pid, base + c.alt_surface * k, surf, seg.text, si, seg.kind)
            pos += 1
    lat.add(pos, pos + 1, 0, 0, 0, "", "", len(reading.segs), "eos")
    return lat, pos


def convert(reading: Reading, dic: Dictionary, n: int = 20, max_pops: int = 50000) -> list[Conversion]:
    """Reading 1つの変換候補を、コストの小さい順に、表記の重複を除いて最大 n 件。"""
    lat, last = _build_lattice(reading, dic)
    num = len(lat.start)
    ends_at: list[list[int]] = [[] for _ in range(last + 2)]
    starts_at: list[list[int]] = [[] for _ in range(last + 2)]
    for v in range(1, num):
        starts_at[lat.start[v]].append(v)
    for v in range(num - 1):
        ends_at[lat.end[v]].append(v)

    matrix, size = dic.matrix, dic.pos_size
    lid, rid, wcost = lat.lid, lat.rid, lat.cost
    alpha = [_INF] * num
    alpha[0] = 0
    for p in range(last + 1):
        best_by_rid: dict[int, float] = {}
        for u in ends_at[p]:
            a = alpha[u]
            r = rid[u]
            if a < best_by_rid.get(r, _INF):
                best_by_rid[r] = a
        if not best_by_rid:
            continue
        lefts = [(r * size, a) for r, a in best_by_rid.items()]
        by_lid: dict[int, float] = {}
        for v in starts_at[p]:
            l = lid[v]
            m = by_lid.get(l)
            if m is None:
                m = min(a + matrix[ro + l] for ro, a in lefts)
                by_lid[l] = m
            alpha[v] = m + wcost[v]

    eos = num - 1
    if alpha[eos] == _INF:
        return []

    # 後ろ向き A*: 状態は「右端から node までの経路（suffix）」と、その左隣の候補一覧の何番目を試すか。
    # 左隣の候補は alpha[u] + 接続 の昇順に並べてあるので、取り出すたびに次の兄弟だけを積めば足りる。
    left_cache: dict[tuple[int, int], list[tuple[float, int, int]]] = {}

    def lefts_of(v: int) -> list[tuple[float, int, int]]:
        key = (lat.start[v], lid[v])
        got = left_cache.get(key)
        if got is None:
            l = lid[v]
            got = sorted((alpha[u] + matrix[rid[u] * size + l], u, matrix[rid[u] * size + l])
                         for u in ends_at[lat.start[v]] if alpha[u] < _INF)
            left_cache[key] = got
        return got

    results: list[Conversion] = []
    seen: set[str] = set()
    tick = 0
    heap: list = []
    root = (eos, None, 0)  # (node, 右側の suffix, node 自身から右端までのコスト)
    cands = lefts_of(eos)
    if cands:
        heap.append((cands[0][0], tick, root, 0))
    pops = 0
    while heap and len(results) < n and pops < max_pops:
        f, _, suffix, idx = heapq.heappop(heap)
        pops += 1
        v, _, beta = suffix
        cands = lefts_of(v)
        if idx + 1 < len(cands):
            tick += 1
            heapq.heappush(heap, (cands[idx + 1][0] + beta, tick, suffix, idx + 1))
        _, u, cn = cands[idx]
        if u == 0:
            words = []
            s = suffix
            while s is not None and s[0] != eos:
                w = s[0]
                words.append(Word(surface=lat.surface[w], reading=lat.reading[w], seg_index=lat.seg[w],
                                  source=lat.source[w], cost=wcost[w], lid=lid[w], rid=rid[w]))
                s = s[1]
            conv = Conversion(reading=reading, words=tuple(words), cost=f)
            if conv.surface not in seen:
                seen.add(conv.surface)
                results.append(conv)
            continue
        new = (u, suffix, wcost[u] + cn + beta)
        nc = lefts_of(u)
        if nc:
            tick += 1
            heapq.heappush(heap, (nc[0][0] + new[2], tick, new, 0))
    return results


# ---- 簡易の Reading（CLI・評価用。本番の Reading は core.reading が作る） ----

_SYM_WIDE = {",": "、", ".": "。", "?": "？", "!": "！", "(": "（", ")": "）", "[": "「", "]": "」",
             ":": "：", "~": "〜", "/": "・", "-": "ー"}


def _char_kind(ch: str) -> str:
    if "ァ" <= ch <= "ヶ" or "ぁ" <= ch <= "ゖ" or ch in "ーヴ":
        return "kana"
    if ch.isascii() and ch.isdigit():
        return "num"
    if ch.isascii() and ch.isalpha():
        return "en"
    return "sym"


def simple_reading(text: str) -> Reading:
    """かな（ひらがな・カタカナ）・英字・数字・記号の文字列を区間に切る。記号は1字ずつ、表記はそのまま（ASCII 記号は全角を先に）。"""
    segs: list[Seg] = []
    i = 0
    while i < len(text):
        kind = _char_kind(text[i])
        j = i + 1
        if kind != "sym":
            while j < len(text) and _char_kind(text[j]) == kind:
                j += 1
        chunk = text[i:j]
        if kind == "kana":
            segs.append(Seg("kana", i, j, hira_to_kata(chunk)))
        elif kind == "en":
            segs.append(Seg("en", i, j, chunk.lower(), (chunk,) if chunk != chunk.lower() else ()))
        elif kind == "num":
            segs.append(Seg("num", i, j, chunk))
        else:
            surfaces = (_SYM_WIDE[chunk], chunk) if chunk in _SYM_WIDE else (chunk,)
            segs.append(Seg("sym", i, j, chunk, surfaces))
        i = j
    return Reading(segs=tuple(segs))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="読み → 表記の N-best（Mozc 辞書）")
    ap.add_argument("text", nargs="+", help="読み（カタカナ・ひらがな。英字・数字・記号を混ぜてよい）")
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--dict", type=Path, default=MOZC_DIR)
    ap.add_argument("--words", action="store_true", help="語ごとの内訳も表示する")
    args = ap.parse_args()

    if not (args.dict / "id.def").exists():
        sys.exit(f"辞書がない: {args.dict}（uv run python -m core.fetch_mozc で取得）")
    dic = Dictionary.load(args.dict)
    print(f"# 辞書の読み込み {dic.stats['load_sec']:.2f} 秒（{dic.stats['source']}）", file=sys.stderr)
    for text in args.text:
        reading = simple_reading(text)
        t0 = time.perf_counter()
        convs = convert(reading, dic, args.n)
        ms = (time.perf_counter() - t0) * 1000
        print(f"## {text}（{ms:.1f} ms）")
        for k, cv in enumerate(convs, 1):
            print(f"{k:3d} {cv.cost:7.0f}  {cv.surface}")
            if args.words:
                print("        " + " | ".join(f"{w.surface}/{w.reading}/{w.source}/{w.lid}:{w.cost}" for w in cv.words))


if __name__ == "__main__":
    main()
