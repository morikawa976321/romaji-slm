"""結果の JSONL と集計 Markdown 表を書く。"""

import json
import os
import platform
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"


def env_info(device: str) -> dict:
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    info = {"cpu": cpu, "logical_cpus": os.cpu_count(), "device": device, "python": platform.python_version()}
    try:
        import torch
        info["torch"] = torch.__version__
        info["torch_threads"] = torch.get_num_threads()
        if device.startswith("cuda") and torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return info


def aggregate(rows: list[dict]) -> list[dict]:
    cats = sorted({r["category"] for r in rows})
    out = []
    for c in [*cats, "ALL"]:
        rs = [r for r in rows if c == "ALL" or r["category"] == c]
        ms = [r["ms"] for r in rs]
        out.append({
            "category": c, "n": len(rs),
            "exact": sum(r["exact"] for r in rs) / len(rs),
            "loose": sum(r["loose"] for r in rs) / len(rs),
            "min_cer": sum(r["min_cer"] for r in rs) / len(rs),
            "ms_mean": statistics.fmean(ms), "ms_median": statistics.median(ms),
            "gen_tokens": statistics.fmean(r["n_gen_tokens"] for r in rs),
            # 候補を返す変換器だけ: 候補のどれかが正解（完全一致）
            "oracle": _mean(r.get("oracle") for r in rs),
            # 正解に英単語を含む件だけ: 正解の英単語のうち出力に残った割合（大小無視）
            "en_kept": _mean(r.get("en_kept") for r in rs),
            # 打鍵ミスの無い件だけ（カテゴリ typo / noisy 以外）: 1位が打鍵を補正した割合
            "miscorrect": _mean(r.get("edited") for r in rs if r["category"] not in _TYPO_CATS),
        })
    return out


_TYPO_CATS = {"typo", "noisy"}


def _mean(values) -> float | None:
    vs = [float(v) for v in values if v is not None]
    return statistics.fmean(vs) if vs else None


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def render_markdown(converter: str, set_name: str, rows: list[dict], env: dict) -> str:
    lines = [
        f"# {converter} / {set_name}", "",
        f"- 日時: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "- 環境: " + "、".join(f"{k}={v}" for k, v in env.items()),
        "- 速度（ms）は上の環境での参考値。要件判定は ThinkPad（Core Ultra 7 255H）上の値だけで行う。", "",
        "| カテゴリ | 件数 | 完全一致 | ゆるい一致 | 平均 MinCER | oracle | 英語保持 | 誤補正 "
        "| ms/件（平均） | ms/件（中央値） | 生成トークン |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in aggregate(rows):
        lines.append(f"| {a['category']} | {a['n']} | {a['exact']:.1%} | {a['loose']:.1%} | {a['min_cer']:.3f} "
                     f"| {_pct(a['oracle'])} | {_pct(a['en_kept'])} | {_pct(a['miscorrect'])} "
                     f"| {a['ms_mean']:.0f} | {a['ms_median']:.0f} | {a['gen_tokens']:.1f} |")
    return "\n".join(lines) + "\n"


def write_results(converter: str, set_name: str, rows: list[dict], env: dict, out_dir: Path = OUT) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Windows のファイル名に使えない文字（: / など）を潰す
    stem = re.sub(r"[^\w.+-]", "_", f"{converter}__{set_name}")
    jsonl, md = out_dir / f"{stem}.jsonl", out_dir / f"{stem}.md"
    jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    md.write_text(render_markdown(converter, set_name, rows, env), encoding="utf-8")
    return jsonl, md
