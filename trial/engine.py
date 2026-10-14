"""体験版サーバの変換エンジン: 設定（trial/configs.json の1項目）ごとに変換する。

- CoreEngine: core.pipeline.CorePipeline を包む。同じ model・device の組はモデルと辞書を1回だけ読む。
- MockEngine: 決定的な偽の結果。torch とモデルが無い環境での動作確認とテストに使う。

convert は {"text", "ms", "finished", "candidates", "error"} を返す。変換が終わらなかったときは
原入力を text に返す（本人の入力を失わない）。例外は呼び出し側（server）が受けて同じ形にする。
"""

import inspect
import random
import re
import sys
import threading
import time
import zlib
from contextlib import contextmanager
from types import SimpleNamespace

# config の項目 → CorePipeline.__init__ の引数。受け付けない引数は渡さない（punct・en_case は core 側で追加予定）
PARAM_MAP = {"model": "model", "device": "device", "k": "k", "w": "w_read", "f": "w_flu",
             "punct": "punct", "en_case": "en_case"}
UNFINISHED = "変換が完了しなかった（候補が無いか、全候補が辞書の制約で止まった）"


class CoreEngine:
    name = "core"

    def __init__(self, pipeline_cls=None):
        self._cls = pipeline_cls
        self._pipes: dict[tuple, object] = {}
        self._shared: dict[tuple, tuple] = {}  # (model, device) → (scorer, dic)
        self._lock = threading.Lock()

    def _pipeline_cls(self):
        if self._cls is None:
            from core.pipeline import CorePipeline
            self._cls = CorePipeline
        return self._cls

    def pipeline_kwargs(self, config: dict) -> tuple[dict, list[str]]:
        """config → CorePipeline の引数と、受け付けられずに捨てた config の項目。"""
        accepted = inspect.signature(self._pipeline_cls().__init__).parameters
        kwargs, ignored = {}, []
        for key, param in PARAM_MAP.items():
            if key not in config:
                continue
            if param in accepted:
                kwargs[param] = config[key]
            else:
                ignored.append(key)
        return kwargs, ignored

    def describe(self, configs: dict[str, dict]) -> dict:
        return {"name": self.name, "ignored": {cid: self.pipeline_kwargs(c)[1] for cid, c in configs.items()}}

    @contextmanager
    def _reuse(self, cls, scorer, dic):
        """CorePipeline.__init__ が読む ZenzScorer と Dictionary.load を、読み込み済みのものに差し替える。

        __init__ を素通しで走らせるので、core 側が引数や属性を増やしても追随できる。差し替え先の名前が
        モジュールに無ければ（core の作りが変わったら）何もせず、毎回読み込む。
        """
        mod = sys.modules.get(cls.__module__)
        names = ("ZenzScorer", "Dictionary")
        if mod is None or not all(hasattr(mod, n) for n in names):
            yield
            return
        saved = {n: getattr(mod, n) for n in names}
        mod.ZenzScorer = lambda *a, **k: scorer
        mod.Dictionary = SimpleNamespace(load=lambda *a, **k: dic)
        try:
            yield
        finally:
            for n, v in saved.items():
                setattr(mod, n, v)

    def _pipeline(self, config: dict):
        kwargs, _ = self.pipeline_kwargs(config)
        key = tuple(sorted(kwargs.items()))
        with self._lock:
            if key in self._pipes:
                return self._pipes[key]
            cls = self._pipeline_cls()
            shared_key = (kwargs.get("model"), kwargs.get("device"))
            if shared_key in self._shared:
                with self._reuse(cls, *self._shared[shared_key]):
                    pipe = cls(**kwargs)
            else:
                pipe = cls(**kwargs)
                if hasattr(pipe, "scorer") and hasattr(pipe, "dic"):
                    self._shared[shared_key] = (pipe.scorer, pipe.dic)
            self._pipes[key] = pipe
            return pipe

    def preload(self, configs: dict[str, dict]) -> None:
        for c in configs.values():
            self._pipeline(c)

    def convert(self, text: str, left_context: str, config: dict) -> dict:
        pipe = self._pipeline(config)
        # convert_detailed の中で呼ばれる candidates() の結果を横取りして、最良の score を見る（2回計算しない）
        captured: list[list[dict]] = []
        orig = pipe.candidates

        def capture(*a, **k):
            r = orig(*a, **k)
            captured.append(r)
            return r

        pipe.candidates = capture
        t0 = time.perf_counter()
        try:
            res = pipe.convert_detailed(text, left_context)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            if pipe.__dict__.get("candidates") is capture:
                del pipe.candidates
        cands = list(res.candidates)
        finished = bool(cands)
        if captured:
            best = captured[-1]
            finished = finished and bool(best) and best[0].get("score", float("-inf")) != float("-inf")
        if not finished:
            return {"text": text, "ms": ms, "finished": False, "candidates": cands, "error": UNFINISHED}
        return {"text": res.text, "ms": ms, "finished": True, "candidates": cands, "error": None}


def _romaji_table() -> tuple[dict[str, str], int]:
    from core.romaji_table import MAXLEN, TABLE
    return TABLE, MAXLEN


def _to_hiragana(romaji: str) -> str:
    """規則だけの粗いローマ字→ひらがな（MockEngine の見た目用）。"""
    table, maxlen = _romaji_table()
    out, i = [], 0
    while i < len(romaji):
        c = romaji[i]
        nxt = romaji[i + 1] if i + 1 < len(romaji) else ""
        if c == "n" and nxt == "n":
            out.append("ン")
            i += 2
            continue
        if c == "n" and (not nxt or nxt not in "aiueoy"):
            out.append("ン")
            i += 1
            continue
        if c == nxt and c.isalpha() and c not in "aiueon":
            out.append("ッ")
            i += 1
            continue
        for L in range(min(maxlen, len(romaji) - i), 0, -1):
            if romaji[i:i + L] in table:
                out.append(table[romaji[i:i + L]])
                i += L
                break
        else:
            out.append({",": "、", ".": "。", "-": "ー", "?": "？", "!": "！"}.get(c, c))
            i += 1
    kata = "".join(out)
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in kata)


class MockEngine:
    """決定的な偽の変換。config の値（k・punct・en_case）ごとに規則を変え、ms は (seed, 設定 id, 入力) から決める。

    入力に `!!err` を含むと例外、`!!unfinished` を含むと未完了を返す（失敗時の表示とテストに使う）。
    delay > 0 なら ms × delay だけ実際に待つ（進捗表示の確認用）。
    """

    name = "mock"

    def __init__(self, seed: int = 0, delay: float = 0.0):
        self.seed, self.delay = seed, delay

    def describe(self, configs: dict[str, dict]) -> dict:
        return {"name": self.name, "ignored": {cid: [] for cid in configs}}

    def preload(self, configs: dict[str, dict]) -> None:
        pass

    def convert(self, text: str, left_context: str, config: dict) -> dict:
        cid = config.get("id", "")
        rng = random.Random(zlib.crc32(f"{self.seed}|{cid}|{text}|{left_context}".encode()))
        ms = round(rng.uniform(40, 1200), 1)
        if self.delay:
            time.sleep(ms / 1000 * self.delay)
        if "!!err" in text:
            raise RuntimeError("mock: 意図した失敗")
        if "!!unfinished" in text:
            return {"text": text, "ms": ms, "finished": False, "candidates": [], "error": UNFINISHED}
        out = _to_hiragana(text)
        # 比べる軸の設定どうしで見た目が変わるように、config の値ごとに規則を変える
        if config.get("k", 5) < 5:
            out = "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in out)
        if config.get("punct") == "ja" and not out.endswith(("。", "？", "！")):
            out += "。"
        elif config.get("punct") == "insert":
            out = out[: len(out) // 2] + "、" + out[len(out) // 2:]
        en = config.get("en_case")
        if en == "registered":
            out = re.sub(r"[a-z]+", lambda m: m.group().capitalize(), out)
        elif en == "auto":
            out = re.sub(r"[a-z]+", lambda m: m.group().upper(), out)
        return {"text": out, "ms": ms, "finished": True, "candidates": [out], "error": None}
