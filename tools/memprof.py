"""変換サーバのメモリを、読み込みの段階ごとと、変換を重ねたときの推移で測る（docs/RESOURCES.md）。Windows 専用。

    uv run python -m tools.memprof out.jsonl [件数=600] [--device cuda|cpu]（cpu は ONNX Runtime 版。常駐入力の GPU の無い機械と同じ）

1 行 1 段階の JSON を書く: ws_mb（作業セット）・private_mb（専有＝確保量）、GPU なら cuda_alloc_mb / cuda_reserved_mb、
推移の行には読み段のキャッシュの項目数（lm_step・lm_end・parses）。入力は eval/sets の romaji を順に使う。
"""

import ctypes
import ctypes.wintypes as W
import gc
import json
import sys
import time


class _PMC(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


# 型を指定しないと 64 ビットのハンドルが壊れ、呼び出しが黙って失敗して 0 が返る
_k32 = ctypes.WinDLL("kernel32")
_k32.GetCurrentProcess.restype = W.HANDLE
_k32.K32GetProcessMemoryInfo.argtypes = [W.HANDLE, ctypes.POINTER(_PMC), W.DWORD]
_k32.K32GetProcessMemoryInfo.restype = W.BOOL


def mem() -> tuple[float, float]:
    """(作業セット MB, 専有 MB)"""
    c = _PMC()
    c.cb = ctypes.sizeof(c)
    if not _k32.K32GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise OSError("K32GetProcessMemoryInfo")
    return c.WorkingSetSize / 2**20, c.PrivateUsage / 2**20


SETS = ["eval/sets/synth_user.jsonl", "eval/sets/gen_test.jsonl", "eval/sets/user_style.jsonl", "eval/sets/typo_hard.jsonl"]


def main(argv: list[str]) -> None:
    device = "cuda"
    if "--device" in argv:
        k = argv.index("--device")
        device = argv[k + 1]
        argv = argv[:k] + argv[k + 2:]
    out = open(argv[0], "w", encoding="utf-8")
    n = int(argv[1]) if len(argv) > 1 else 600
    torch = None

    def log(stage, **kw):
        ws, pv = mem()
        if torch is not None and device == "cuda":
            kw.update(cuda_alloc_mb=round(torch.cuda.memory_allocated() / 2**20),
                      cuda_reserved_mb=round(torch.cuda.memory_reserved() / 2**20))
        out.write(json.dumps({"stage": stage, "ws_mb": round(ws), "private_mb": round(pv), **kw}, ensure_ascii=False) + "\n")
        out.flush()

    log("python")
    import torch as _torch
    log("import torch")
    torch = _torch
    from core import reading as R
    from core.kanakanji import Dictionary
    from core.pipeline import CorePipeline
    log("import core")
    R.readings("kyouhaiitenkidesune")
    log("読み段の初回（読みの LM・英単語・編集と語の索引）")
    d = Dictionary.load()
    log("Mozc の辞書")
    del d
    gc.collect()
    if device == "cuda":
        torch.zeros(1, device="cuda")
        log("CUDA の文脈")
    if device == "cuda":
        p = CorePipeline(device=device)
        log("CorePipeline（transformers・zenz の重み・辞書）", params=sum(x.numel() for x in p.scorer.model.parameters()))
    else:
        from core.ort_backend import OrtCorePipeline
        p = OrtCorePipeline(threads=4)
        log("OrtCorePipeline（ONNX Runtime・zenz の重み・辞書）")

    rows = []
    for f in SETS:
        rows += [json.loads(line)["romaji"] for line in open(f, encoding="utf-8") if line.strip()]
    step = max(1, n // 8)
    t0 = time.perf_counter()
    for i, keys in enumerate(rows[:n], 1):
        p.convert_nbest(keys, "")
        if i % step == 0 or i == n:
            kw = {"sec": round(time.perf_counter() - t0), "lm_step": R._lm_step.cache_info().currsize,
                  "lm_end": R._lm_end.cache_info().currsize, "parses": R._parses_as_romaji.cache_info().currsize}
            if device == "cuda":
                kw["cuda_peak_mb"] = round(torch.cuda.max_memory_allocated() / 2**20)
            log(f"{i} 件の変換の後", **kw)
    R._lm_step.cache_clear()
    R._lm_end.cache_clear()
    R._parses_as_romaji.cache_clear()
    gc.collect()
    log("読み段のキャッシュを消した後")
    if device == "cuda":
        torch.cuda.empty_cache()
        log("torch.cuda.empty_cache の後")


if __name__ == "__main__":
    main(sys.argv[1:])
