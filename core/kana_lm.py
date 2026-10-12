"""カタカナ読みの文字 4-gram（読みラティスで、打鍵ミスの直し方どうしを比べるための「読みらしさ」）。

学習元（出典とライセンスは docs/READING.md）:
- 京都大学 Wikipedia 注釈コーパス（CC BY-SA 4.0）の train・dev 3779 文書の、人手で付けた形態素の読み。
  かな以外の形態素（英数字・記号）で区切り、区切りごとに文頭・文末を付ける（読みラティスも英数字・記号の後で文脈を切る）
- Mozc の dictionary_oss（BSD-3、IPAdic 由来部分を含む）の読み。同じ読みは最小コストだけ残し、
  重み 3·exp(-cost/1000)（Mozc のコストは小さいほど頻出）。コーパスに無い語の綴りを補う

Mozc の重みは data/fixtures/zenz_like.jsonl の読み 205 文の 1 文字あたり交差エントロピーで、次数は
WAC test（学習に使っていない）での読みラティスの打鍵ミス復元率で選んだ（docs/READING.md）。
平滑化は Witten-Bell の補間。

    uv run python -m core.kana_lm build   # models/reading/kana_lm.pkl を作る（素材が無ければ github から取る）
"""

import math
import pickle
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOZC_DIR = ROOT / "models" / "mozc"
MOZC_URL = "https://raw.githubusercontent.com/google/mozc/master/src/data/dictionary_oss/dictionary{:02d}.txt"
WAC_DIR = ROOT / "models" / "wac"
WAC_URL = "https://raw.githubusercontent.com/ku-nlp/WikipediaAnnotatedCorpus/main/"
LM_PATH = ROOT / "models" / "reading" / "kana_lm.pkl"
ORDER = 4
TAU = 1000.0
MOZC_WEIGHT = 3.0
_KANA_RUN = re.compile(r"^[ァ-ヴー]+$")
BOS = "^"
EOS = "$"


def to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def download_mozc(d: Path = MOZC_DIR) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        p = d / f"dictionary{i:02d}.txt"
        if not p.exists():
            with urllib.request.urlopen(MOZC_URL.format(i), timeout=120) as r:
                p.write_bytes(r.read())


def mozc_readings(d: Path = MOZC_DIR) -> dict[str, int]:
    """読み（カタカナ）→ 最小コスト。"""
    if not (d / "dictionary09.txt").exists():
        download_mozc(d)
    best: dict[str, int] = {}
    for p in sorted(d.glob("dictionary0*.txt")):
        for line in p.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            r, c = to_katakana(cols[0]), int(cols[3])
            if r not in best or c < best[r]:
                best[r] = c
    return best


def _get(url: str) -> bytes:
    for k in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except OSError:
            if k == 3:
                raise
    raise AssertionError


def download_wac(d: Path = WAC_DIR, splits: tuple[str, ...] = ("train", "dev")) -> list[Path]:
    """Wikipedia 注釈コーパスを1文書ずつ取る（github の tarball は環境によって届かない）。
    test は学習に使わず、読みラティスのコスト調整用（eval/reading_bench.py --set wac-test）に取っておく。"""
    (d / "knp").mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for split in splits:
        p = d / f"{split}.id"
        if not p.exists():
            p.write_bytes(_get(WAC_URL + f"id/{split}.id"))
        ids += p.read_text().split()

    def one(doc: str) -> Path:
        p = d / "knp" / f"{doc}.knp"
        if not p.exists() or p.stat().st_size == 0:
            p.write_bytes(_get(WAC_URL + f"knp/{doc[:8]}/{doc}.knp"))
        return p

    with ThreadPoolExecutor(16) as ex:
        return list(ex.map(one, ids))


def wac_sentences(splits: tuple[str, ...] = ("train", "dev"), d: Path = WAC_DIR) -> list[list[tuple[str, str]]]:
    """各文の形態素の (表層, 読み)。"""
    out: list[list[tuple[str, str]]] = []
    for p in sorted(download_wac(d, splits)):
        cur: list[tuple[str, str]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "*", "+")):
                continue
            if line.startswith("EOS"):
                out.append(cur)
                cur = []
            else:
                f = line.split(" ")
                cur.append((f[0], f[1]))
    return out


def wac_runs(d: Path = WAC_DIR) -> list[str]:
    """コーパスの各文の読みを、かな以外の形態素で区切った連続（カタカナ）。"""
    runs: list[str] = []
    for p in sorted(download_wac(d)):
        cur: list[str] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "*", "+")):
                continue
            r = "" if line.startswith("EOS") else to_katakana(line.split(" ")[1])
            if _KANA_RUN.match(r):
                cur.append(r)
            elif cur:
                runs.append("".join(cur))
                cur = []
    return runs


class KanaLM:
    """Witten-Bell 補間の n-gram。学習時に、現れた n-gram の補間済み log P と、文脈ごとのバックオフ重み
    log(T/(C+T)) を表にしておき、実行時は辞書を引くだけにする（読みラティスは1文で数十万回引く）。"""

    def __init__(self, lp: list[dict], bow: list[dict], vocab: int):
        self.lp, self.bow, self.unk = lp, bow, -math.log(vocab)
        self._memo: dict[tuple[str, str], float] = {}

    def logp(self, hist: str, c: str) -> float:
        """log P(c | hist の末尾 ORDER-1 字)。"""
        hist = hist[len(hist) - ORDER + 1:]
        key = (hist, c)
        v = self._memo.get(key)
        if v is not None:
            return v
        acc = 0.0
        for n in range(ORDER, 0, -1):
            h = hist[len(hist) - n + 1:] if n > 1 else ""
            if len(h) < n - 1:
                continue
            v = self.lp[n].get(h + c)
            if v is not None:
                break
            acc += self.bow[n].get(h, 0.0)
        else:
            v = self.unk
        v = self._memo[key] = acc + v
        return v

    @classmethod
    def from_counts(cls, cnt: list[dict], ctx: list[dict], types: list[dict], vocab: int) -> "KanaLM":
        lp: list[dict] = [{} for _ in range(ORDER + 1)]
        bow: list[dict] = [{} for _ in range(ORDER + 1)]
        for n in range(1, ORDER + 1):
            for h, total in ctx[n].items():
                bow[n][h] = math.log(types[n][h] / (total + types[n][h]))
        for n in range(1, ORDER + 1):
            for g, k in cnt[n].items():
                h = g[:-1]
                lower = math.exp(lp[n - 1][g[1:]]) if n > 1 else 1.0 / vocab
                t, total = types[n][h], ctx[n][h]
                lp[n][g] = math.log((k + t * lower) / (total + t))
        return cls(lp, bow, vocab)


def train(weighted: dict[str, float]) -> KanaLM:
    cnt = [defaultdict(float) for _ in range(ORDER + 1)]
    ctx = [defaultdict(float) for _ in range(ORDER + 1)]
    seen = [defaultdict(set) for _ in range(ORDER + 1)]
    for r, w in weighted.items():
        s = BOS * (ORDER - 1) + r + EOS
        for i in range(ORDER - 1, len(s)):
            for n in range(1, ORDER + 1):
                h = s[i - n + 1:i]
                cnt[n][h + s[i]] += w
                ctx[n][h] += w
                seen[n][h].add(s[i])
    types = [{h: len(v) for h, v in t.items()} for t in seen]
    return KanaLM.from_counts(cnt, ctx, types, len(seen[1][""]) + 1)


def build(path: Path = LM_PATH) -> KanaLM:
    weighted: Counter = Counter(wac_runs())
    for r, c in mozc_readings().items():
        weighted[r] += MOZC_WEIGHT * math.exp(-c / TAU)
    lm = train(weighted)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        pickle.dump({"lp": lm.lp, "bow": lm.bow, "unk": lm.unk}, f, protocol=pickle.HIGHEST_PROTOCOL)
    return lm


@lru_cache(maxsize=1)
def load(path: Path = LM_PATH) -> KanaLM:
    if not path.exists():
        return build(path)
    with path.open("rb") as f:
        d = pickle.load(f)
    lm = KanaLM(d["lp"], d["bow"], 1)
    lm.unk = d["unk"]
    return lm


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
        print(f"書き出し: {LM_PATH}")
    else:
        print(__doc__)
