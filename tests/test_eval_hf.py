"""HFCausalLM を、ランダム初期化の極小 GPT-2 と文字単位トークナイザで通す（重みの取得は要らない）。"""

import string

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

from eval.converters import HFCausalLM  # noqa: E402
from eval.prompts import CONTEXT, INPUT, OUTPUT  # noqa: E402


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory):
    from tokenizers import Regex, Tokenizer, models, pre_tokenizers
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast

    chars = list(string.ascii_lowercase + string.digits + ",.-?!") + [INPUT, OUTPUT, CONTEXT, "キ", "ョ", "ウ", "今", "日"]
    specials = ["[UNK]", "[PAD]", "</s>"]
    vocab = {t: i for i, t in enumerate(specials + chars)}
    tk = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.Split(Regex("."), "isolated")
    tok = PreTrainedTokenizerFast(tokenizer_object=tk, unk_token="[UNK]", pad_token="[PAD]", eos_token="</s>")
    cfg = GPT2Config(vocab_size=len(vocab), n_embd=16, n_layer=1, n_head=2, n_positions=256,
                     eos_token_id=vocab["</s>"], bos_token_id=vocab["</s>"])
    path = tmp_path_factory.mktemp("tiny")
    GPT2LMHeadModel(cfg).save_pretrained(path)
    tok.save_pretrained(path)
    return str(path)


def test_convert_interface(tiny_model, torch_device):
    conv = HFCausalLM(tiny_model, "zenz-v3-romaji", torch_device)
    r = conv.convert_detailed("kyou", "ab")
    assert r.prompt == f"{CONTEXT}ab{INPUT}kyou{OUTPUT}"
    # 文字単位なので、プロンプトのトークン数は文字数に等しい（特殊トークンを足さない）
    assert r.n_prompt_tokens == len(r.prompt)
    assert 1 <= r.n_gen_tokens <= 2 * len("kyou") + 16
    assert isinstance(conv.convert("kyou"), str)


def test_baseline_template_converts_to_katakana(tiny_model, torch_device):
    r = HFCausalLM(tiny_model, "zenz-v3", torch_device).convert_detailed("kyou", "")
    assert r.prompt == f"{INPUT}キョウ{OUTPUT}"
