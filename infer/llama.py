"""llama.cpp の llama-server を子プロセスで立て、HTTP で変換する。

llama-cpp-python ではなく llama-server を使う理由は docs/INFER.md「backend llama の方式」。
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from eval.converters import Result
from eval.prompts import TEMPLATES, TWO_STAGE, build_prompt, split_two_stage

ENV_BIN = "LLAMA_CPP_BIN"  # llama-server などの実行ファイルがあるディレクトリ
ENV_DIR = "LLAMA_CPP_DIR"  # llama.cpp のソースのチェックアウト（変換スクリプトとビルド結果）


def find_binary(name: str, bin_dir: str | Path | None = None, llama_cpp_dir: str | Path | None = None) -> Path:
    """`llama-server` などを探す。順に bin_dir、$LLAMA_CPP_BIN、llama.cpp のビルド出力、PATH。"""
    exe = name + (".exe" if os.name == "nt" else "")
    dirs: list[Path] = []
    if bin_dir:
        dirs.append(Path(bin_dir))
    if os.environ.get(ENV_BIN):
        dirs.append(Path(os.environ[ENV_BIN]))
    for root in (llama_cpp_dir, os.environ.get(ENV_DIR)):
        if root:
            # Visual Studio のジェネレータは構成名の下に出す
            dirs += [Path(root) / "build" / "bin", Path(root) / "build" / "bin" / "Release", Path(root)]
    for d in dirs:
        if (d / exe).is_file():
            return d / exe
    found = shutil.which(exe)
    if found:
        return Path(found)
    raise FileNotFoundError(
        f"{exe} が見つからない。--llama-bin で場所を指すか、環境変数 {ENV_BIN} を設定する（docs/INFER.md）")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    """llama-server への HTTP クライアント。`url` を渡せば既に動いているサーバーに繋ぎ、渡さなければ自前で起動する。"""

    def __init__(self, model: str | Path | None = None, *, url: str | None = None, bin_dir: str | Path | None = None,
                 threads: int | None = None, n_gpu_layers: int | None = None, extra_args: list[str] | None = None,
                 startup_timeout: float = 120.0, n_slots: int = 1):
        self.proc: subprocess.Popen | None = None
        self._log = None
        if url:
            self.url = url.rstrip("/")
        else:
            if model is None:
                raise ValueError("model か url のどちらかが要る")
            if not Path(model).is_file():
                raise FileNotFoundError(f"GGUF がない: {model}")
            port = _free_port()
            self.url = f"http://127.0.0.1:{port}"
            # -c 0: 文脈長をモデルから取る。GPT-2 は位置埋め込みが学習時の長さ（zenz は 1024）しかなく、
            # それを超える文脈長で立てると範囲外を読む。-np: 並列に処理するスロットの数（infer は1本ずつなので 1、
            # core.llama_backend は読みの k 件を並べて投げる）
            cmd = [str(find_binary("llama-server", bin_dir)), "-m", str(model), "--host", "127.0.0.1",
                   "--port", str(port), "-c", "0", "-np", str(n_slots), "--no-webui"]
            if threads:
                cmd += ["-t", str(threads)]
            if n_gpu_layers is not None:
                cmd += ["-ngl", str(n_gpu_layers)]
            if n_gpu_layers == 0:
                # CUDA ビルドは -ngl 0 でも既定の --op-offload で一部の演算を GPU に回す。CPU だけにするには機器を外す
                cmd += ["--device", "none"]
            cmd += extra_args or []
            self._log = tempfile.TemporaryFile()
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
        self._wait_ready(startup_timeout)
        props = self._request("GET", "/props")
        self.n_ctx = int(props.get("default_generation_settings", {}).get("n_ctx") or 0)

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 60.0) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # サーバーはエラーの理由を本文に書く。そのままだと「500」しか見えないので理由に足す。
            # 型は HTTPError のまま（_wait_ready が読み込み中の 503 を URLError として待つ）
            detail = e.read().decode(errors="replace")[:500]
            raise urllib.error.HTTPError(e.url, e.code, f"{e.msg}: {path} {detail}", e.hdrs, None) from e

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"llama-server が終了した（コード {self.proc.returncode}）:\n{self.log_tail()}")
            try:
                # 読み込み中は 503 を返す
                self._request("GET", "/health", timeout=2)
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.1)
        self.close()
        raise TimeoutError(f"llama-server が {timeout:.0f} 秒で応答しない: {self.url}")

    def log_tail(self, n: int = 40) -> str:
        if self._log is None:
            return ""
        self._log.seek(0)
        return "\n".join(self._log.read().decode(errors="replace").splitlines()[-n:])

    def tokenize(self, text: str) -> list[int]:
        """`/completion` と同じ条件（特殊トークンを足す・解釈する）でトークン化する。"""
        return self._request("POST", "/tokenize", {"content": text, "add_special": True, "parse_special": True})["tokens"]

    def complete(self, prompt: str, n_predict: int) -> dict:
        """貪欲法で EOS（または n_predict）まで生成する。"""
        return self._request("POST", "/completion", {
            "prompt": prompt, "n_predict": n_predict, "temperature": 0, "top_k": 1, "samplers": ["top_k"],
            "cache_prompt": True, "return_tokens": True,
        })

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class LlamaCppLM:
    """eval.converters.HFCausalLM と同じ口（convert / convert_detailed）を llama-server で実装したもの。"""

    def __init__(self, model: str | Path | None, template: str, *, url: str | None = None, name: str | None = None,
                 **server_kwargs):
        if template not in TEMPLATES:
            raise ValueError(f"未知の雛形: {template}")
        self.template = template
        self.name = name or f"{Path(model).name if model else url}+{template}"
        self.server = LlamaServer(model, url=url, **server_kwargs)

    def convert_detailed(self, romaji: str, left_context: str = "") -> Result:
        prompt = build_prompt(self.template, romaji, left_context)
        cap = (3 if self.template in TWO_STAGE else 2) * len(romaji) + 16  # HFCausalLM と同じ上限
        n_ctx = self.server.n_ctx
        # バイト単位 BPE なのでトークン数 ≤ UTF-8 のバイト数。収まらないおそれがあるときだけ実際に数える
        if n_ctx and len(prompt.encode()) + cap > n_ctx:
            n_prompt = len(self.server.tokenize(prompt))
            cap = min(cap, n_ctx - n_prompt)
            if cap < 1:
                raise ValueError(f"プロンプトが文脈長 {n_ctx} を超える（{n_prompt} トークン）")
        r = self.server.complete(prompt, cap)
        n_gen = int(r.get("tokens_predicted", len(r.get("tokens", []))))
        text = r["content"]
        if self.template in TWO_STAGE:
            text = split_two_stage(text)[1]
        return Result(text, int(r.get("tokens_evaluated", 0)), n_gen, prompt)

    def convert(self, romaji: str, left_context: str = "") -> str:
        return self.convert_detailed(romaji, left_context).text

    def close(self) -> None:
        self.server.close()
