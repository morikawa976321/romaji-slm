"""infer: CLI・GGUF 変換・HF と GGUF のトークン列の一致。

重みもトークナイザも手元で作る（ネットワーク不要）: ランダム初期化の極小 GPT-2 と、zenz と同じ種類の
トークナイザ（GPT2Tokenizer、バイト単位 BPE、空白・改行のバイトを語彙に持たない）。
llama.cpp を使うテストは、環境変数 LLAMA_CPP_DIR が convert_hf_to_gguf.py とビルド済みの
llama-server・llama-quantize のあるチェックアウトを指すときだけ動く（docs/INFER.md「テスト」）。
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

from eval.prompts import CONTEXT, INPUT, OUTPUT, build_prompt  # noqa: E402
from infer import cli, to_gguf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPECIALS = ["[UNK]", "[PAD]", "<s>", "</s>"]  # zenz（ku-nlp/gpt2-small-japanese-char）と同じ並び

CORPUS = [
    "今日はいい天気ですね。", "明日の会議、何時からだっけ？", "GitHubにpushしておいて。", "プルリクエストを作ってくれ。",
    "実験の結果を論文にまとめる。", "ありがとうございます！", "東京都の人口は約1400万人です。", "ｶﾀｶﾅとＡＢＣ１２３、𠮷野家😀",
    "kyouhaiitenkidesune.", "ashitanokaigi,nanjikaradakke?", "githubnipushshiteoite.", "pullrequestwotsukuttekure.",
    "jikkennnokekkaworonnbunnnimatomeru.", "arigatougozaimasu!", "konnnichiha", "0123456789",
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~", INPUT + CONTEXT + OUTPUT,
]


def make_zenz_like_tokenizer(path: Path, vocab_size: int = 400):
    """バイト単位 BPE を学習し、GPT2Tokenizer（vocab.json + merges.txt）として保存する。

    zenz と同じく、空白・改行・CR のバイトを語彙に入れない（エンコードで黙って消える）。
    zenz の語彙はほぼ文字単位だが、ここでは複数文字の結合（`kyou` など）も学習させ、pre-tokenizer の境界を厳しく試す。
    """
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import AutoTokenizer

    alphabet = [c for c in pre_tokenizers.ByteLevel.alphabet() if c not in ("Ġ", "Ċ", "č")]
    tk = Tokenizer(models.BPE())
    tk.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tk.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(vocab_size=vocab_size, special_tokens=SPECIALS, initial_alphabet=alphabet,
                                  show_progress=False)
    tk.train_from_iterator(CORPUS * 20, trainer)
    path.mkdir(parents=True, exist_ok=True)
    # zenz と同じ置き方: vocab.json・merges.txt・tokenizer.json と、GPT2Tokenizer を名乗る tokenizer_config.json
    tk.model.save(str(path))
    tk.save(str(path / "tokenizer.json"))
    (path / "tokenizer_config.json").write_text(json.dumps({
        "tokenizer_class": "GPT2Tokenizer", "add_bos_token": False, "add_prefix_space": False,
        "unk_token": "[UNK]", "pad_token": "[PAD]", "bos_token": "<s>", "eos_token": "</s>",
    }))
    return AutoTokenizer.from_pretrained(path)


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory) -> Path:
    from transformers import GPT2Config, GPT2LMHeadModel

    path = tmp_path_factory.mktemp("zenz-like")
    tok = make_zenz_like_tokenizer(path)
    # n_embd は k-quant（Q5_K_M）のブロック長 256 の倍数にする
    cfg = GPT2Config(vocab_size=len(tok), n_embd=256, n_layer=2, n_head=4, n_positions=256,
                     bos_token_id=tok.bos_token_id, eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
    import torch
    torch.manual_seed(0)
    GPT2LMHeadModel(cfg).save_pretrained(path)
    return path


def test_tokenizer_is_zenz_like(tiny_model):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(tiny_model)
    assert tok.convert_ids_to_tokens(list(range(4))) == SPECIALS
    assert tok("a b\nc", add_special_tokens=False).input_ids == tok("abc", add_special_tokens=False).input_ids
    # zenz の特殊文字は UTF-8 の3バイトに分かれる（語彙にない文字。docs/EVAL.md の語彙調査と同じ）
    assert len(tok(OUTPUT, add_special_tokens=False).input_ids) >= 1
    assert to_gguf.check_tokenizer(tiny_model).startswith("tokenizer.json")


def test_check_tokenizer_rejects_other_pre_tokenizers(tmp_path):
    base = {"model": {"type": "BPE", "vocab": {}, "merges": []}, "normalizer": None}
    for pre in [{"type": "Metaspace"}, {"type": "ByteLevel", "use_regex": False},
                {"type": "ByteLevel", "add_prefix_space": True}]:
        (tmp_path / "tokenizer.json").write_text(json.dumps({**base, "pre_tokenizer": pre}))
        with pytest.raises(ValueError):
            to_gguf.check_tokenizer(tmp_path)
    (tmp_path / "tokenizer.json").write_text(json.dumps({**base, "normalizer": {"type": "NFKC"},
                                                         "pre_tokenizer": {"type": "ByteLevel"}}))
    with pytest.raises(ValueError):
        to_gguf.check_tokenizer(tmp_path)


def test_parse_args_backend():
    assert cli.parse_args(["x", "--model", "m.gguf"]).backend == "llama"
    assert cli.parse_args(["x", "--model", "models/zenz"]).backend == "hf"
    a = cli.parse_args(["x"])
    assert (a.backend, a.model) == ("hf", str(cli.DEFAULT_HF_MODEL))
    a = cli.parse_args(["x", "--server-url", "http://127.0.0.1:8080"])
    assert (a.backend, a.model) == ("llama", None)


def test_cli_hf_single(tiny_model, capsys):
    assert cli.main(["kyou", "--context", "今日", "--backend", "hf", "--model", str(tiny_model)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def _run_cli(args: list[str], stdin: str) -> list[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, "-m", "infer", *args], input=stdin.encode(), capture_output=True,
                       cwd=ROOT, env=env, timeout=300)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return p.stdout.decode().split("\n")[:-1]


def test_cli_hf_stdin(tiny_model):
    # 1行1変換。空行は空行のまま、`左文脈<TAB>ローマ字` は左文脈つき
    out = _run_cli(["--backend", "hf", "--model", str(tiny_model)], "kyou\n\n今日は\tashita\n")
    assert len(out) == 3 and out[1] == ""


# ---- llama.cpp を使うテスト ----

LLAMA_CPP_DIR = os.environ.get("LLAMA_CPP_DIR")
needs_llama = pytest.mark.skipif(
    not (LLAMA_CPP_DIR and (Path(LLAMA_CPP_DIR) / "convert_hf_to_gguf.py").is_file()),
    reason="LLAMA_CPP_DIR（llama.cpp のチェックアウトとビルド）がない")


@pytest.fixture(scope="module")
def ggufs(tiny_model, tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("gguf")
    # --check: 3形式とも HF とトークン列が一致しなければ 1 を返す
    assert to_gguf.main([str(tiny_model), "--llama-cpp", LLAMA_CPP_DIR, "--outdir", str(out), "--check"]) == 0
    paths = {t: out / f"{tiny_model.name}-{q}.gguf" for t, q in to_gguf.QUANT_TYPES.items()}
    paths["f32"] = out / f"{tiny_model.name}-F32.gguf"
    to_gguf.convert(tiny_model, paths["f32"], "f32", Path(LLAMA_CPP_DIR))
    assert all(p.is_file() for p in paths.values())
    return paths


def _gguf_metadata(path: Path) -> dict:
    sys.path.insert(0, str(Path(LLAMA_CPP_DIR) / "gguf-py"))
    import gguf
    r = gguf.GGUFReader(path)
    keys = ["tokenizer.ggml.pre", "tokenizer.ggml.model", "tokenizer.ggml.eos_token_id", "tokenizer.ggml.add_bos_token"]
    return {k: r.fields[k].contents() for k in keys if k in r.fields}


@needs_llama
def test_gguf_metadata(ggufs, tiny_model):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(tiny_model)
    meta = _gguf_metadata(ggufs["q5_k_m"])
    assert meta["tokenizer.ggml.pre"] == "gpt-2"
    assert meta["tokenizer.ggml.model"] == "gpt2"
    assert meta["tokenizer.ggml.eos_token_id"] == tok.eos_token_id
    # 付いていると llama-server が BOS を前置し、HF（add_special_tokens=False）と食い違う
    assert meta.get("tokenizer.ggml.add_bos_token") in (None, False)


def _random_texts(n: int, seed: int = 0) -> list[str]:
    rng = random.Random(seed)
    pool = list("".join(CORPUS)) + list(" \n\r\t　,.!?'") + ["'s", "'ll", "kyou", "😀", "𠮷"]
    return ["".join(rng.choice(pool) for _ in range(rng.randint(1, 40))) for _ in range(n)]


@needs_llama
@pytest.mark.parametrize("qtype", ["f16", "q5_k_m"])
def test_tokenization_matches_hf(ggufs, tiny_model, qtype):
    texts = to_gguf.sample_texts() + _random_texts(300) + [
        build_prompt("zenz-v3-romaji", r, c) for r, c in [("kyou", ""), ("ashita?", "今日は雨。"), ("github", "a b")]]
    bad = to_gguf.compare_tokenization(ggufs[qtype], tiny_model, texts)
    assert bad == [], bad[:3]


@needs_llama
def test_greedy_generation_matches_hf(ggufs, tiny_model):
    """f32 の GGUF なら、貪欲法の生成トークン列が HF と一致する。"""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from infer.llama import LlamaServer
    tok = AutoTokenizer.from_pretrained(tiny_model)
    model = AutoModelForCausalLM.from_pretrained(tiny_model).eval()
    with LlamaServer(ggufs["f32"]) as server:
        for romaji, ctx in [("kyouhaiitenki", ""), ("githubnipushshiteoite", "今日は")]:
            prompt = build_prompt("zenz-v3-romaji", romaji, ctx)
            ids = tok(prompt, return_tensors="pt", add_special_tokens=False).input_ids
            with torch.no_grad():
                hf = model.generate(ids, max_new_tokens=12, do_sample=False, eos_token_id=tok.eos_token_id,
                                    pad_token_id=tok.pad_token_id)[0][ids.shape[1]:].tolist()
            r = server.complete(prompt, 12)
            assert r["tokens_evaluated"] == ids.shape[1]
            ll = r["tokens"]
            assert ll == hf[:len(ll)] and (len(ll) == len(hf) or hf[len(ll)] == tok.eos_token_id)


@needs_llama
def test_cli_llama(ggufs):
    # llama-server は $LLAMA_CPP_DIR のビルド出力から探す
    args = ["--model", str(ggufs["q8_0"])]
    out = _run_cli(args, "kyou\n\n今日は\tashita\n")
    assert len(out) == 3 and out[1] == ""
    assert len(_run_cli(["kyou", "--context", "今日", *args], "")) == 1


@needs_llama
def test_llama_converter_interface(ggufs):
    from infer.llama import LlamaCppLM
    conv = LlamaCppLM(ggufs["q8_0"], "zenz-v3-romaji")
    try:
        r = conv.convert_detailed("kyou", "ab")
        assert r.prompt == f"{CONTEXT}ab{INPUT}kyou{OUTPUT}"
        assert 1 <= r.n_gen_tokens <= 2 * len("kyou") + 16
        # 既に動いているサーバーに繋ぐ（--server-url）
        other = LlamaCppLM(None, "zenz-v3-romaji", url=conv.server.url)
        assert other.convert("kyou", "ab") == r.text
    finally:
        conv.close()
