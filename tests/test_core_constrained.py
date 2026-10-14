"""制約付き生成（core/constrained.py）: k 件をまとめたバッチ生成が、1件ずつの生成と同じ結果を返すこと。

重みはランダム初期化の極小 GPT-2（zenz と同じバイト単位 BPE のトークナイザ）。辞書は手書きの小さな fixture。
バッチ版は左パディングするので、プロンプトの長さが違う読みを混ぜ、位置埋め込みのずれが結果に出ないことを見る。
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

from core.constrained import ConstrainedZenz  # noqa: E402
from core.kanakanji import Dictionary  # noqa: E402
from core.select import ZenzScorer  # noqa: E402
from core.types import Reading, Seg  # noqa: E402
from eval.prompts import CONTEXT, INPUT, OUTPUT  # noqa: E402

POS = ["BOS/EOS,*,*,*,*,*,*", "名詞,一般,*,*,*,*,*", "助詞,係助詞,*,*,*,*,は", "名詞,数,アラビア数字,*,*,*,*",
       "記号,句点,*,*,*,*,*", "記号,読点,*,*,*,*,*"]
NOUN, WA = 1, 2
ENTRIES = [("きょう", NOUN, NOUN, 3000, "今日"), ("きょう", NOUN, NOUN, 4000, "京"), ("は", WA, WA, 500, "は"),
           ("てんき", NOUN, NOUN, 3000, "天気"), ("てんき", NOUN, NOUN, 3500, "転機"), ("てん", NOUN, NOUN, 3000, "点"),
           ("き", NOUN, NOUN, 3000, "木"), ("いい", NOUN, NOUN, 3000, "良い"), ("ね", WA, WA, 800, "ね")]
CORPUS = ["キョウハイイテンキデスネ", "今日は良い天気ですね", "京", "転機", "点", "木", "きょうはてんき", "ハ", "ネ",
          "キョウ", "テンキ", "イイ", "デス", "。", "、", INPUT + CONTEXT + OUTPUT, "abcdefghijklmnopqrstuvwxyz"]


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory) -> str:
    import torch
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import GPT2Config, GPT2LMHeadModel

    path = tmp_path_factory.mktemp("zenz-like-constrained")
    alphabet = [c for c in pre_tokenizers.ByteLevel.alphabet() if c not in ("Ġ", "Ċ", "č")]
    tk = Tokenizer(models.BPE())
    tk.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tk.decoder = decoders.ByteLevel()
    specials = ["[UNK]", "[PAD]", "<s>", "</s>"]
    tk.train_from_iterator(CORPUS * 20, trainers.BpeTrainer(vocab_size=400, special_tokens=specials,
                                                            initial_alphabet=alphabet, show_progress=False))
    tk.model.save(str(path))
    tk.save(str(path / "tokenizer.json"))
    (path / "tokenizer_config.json").write_text(json.dumps({
        "tokenizer_class": "GPT2Tokenizer", "add_bos_token": False, "add_prefix_space": False,
        "unk_token": "[UNK]", "pad_token": "[PAD]", "bos_token": "<s>", "eos_token": "</s>"}))
    cfg = GPT2Config(vocab_size=tk.get_vocab_size(), n_embd=64, n_layer=2, n_head=4, n_positions=256,
                     bos_token_id=2, eos_token_id=3, pad_token_id=1)
    torch.manual_seed(0)
    GPT2LMHeadModel(cfg).save_pretrained(path)
    return str(path)


@pytest.fixture(scope="module")
def cz(tiny_model):
    return ConstrainedZenz(ZenzScorer(tiny_model, "cpu"))


@pytest.fixture
def dic():
    m = [[1000] * len(POS) for _ in POS]
    m[0][0] = 0
    return Dictionary.from_entries(ENTRIES, m, POS)


def _kana(text: str) -> Reading:
    return Reading((Seg("kana", 0, len(text), text),))


READINGS = [_kana("キョウ"), _kana("キョウハテンキ"), _kana("テンキハキョウハイイ"), _kana("キ"),
            Reading((Seg("kana", 0, 3, "キョウ"), Seg("sym", 3, 4, ".", ("。", "."))))]


@pytest.mark.parametrize("left_context", ["", "今日は良い天気ですね"])
def test_batch_matches_sequential(cz, dic, left_context):
    single = [cz.decode_scored(r, dic, left_context) for r in READINGS]
    batch = cz.decode_batch(READINGS, dic, left_context)
    assert len(batch) == len(single)
    for (bt, blp, bf), (st, slp, sf) in zip(batch, single):
        assert bt == st
        assert bf == sf
        assert blp == pytest.approx(slp, abs=1e-3)


def test_outputs_stay_on_lattice(cz, dic):
    # 出力は辞書の語・かなのまま・記号の候補だけでできている（読みで説明できない表層は出ない）
    for text, _, finished in cz.decode_batch(READINGS, dic):
        assert all(ch in "今日京は天気転機点木良いねキョウハテンキイイきょうはてんきい。." for ch in text), text


def test_batch_order_and_empty(cz, dic):
    assert cz.decode_batch([], dic) == []
    one = cz.decode_batch([READINGS[1]], dic)[0]
    assert one[0] == cz.decode_scored(READINGS[1], dic)[0]
    # 並び順を変えても各読みの結果は変わらない
    rev = cz.decode_batch(READINGS[::-1], dic)
    fwd = cz.decode_batch(READINGS, dic)
    assert [t for t, _, _ in rev] == [t for t, _, _ in fwd][::-1]
