"""ONNX Runtime 版（core/ort_backend.py）が torch 版（GPU）と同じ量を返すこと。

重みはランダム初期化の極小 GPT-2、辞書は tests/test_core_constrained.py の手書きの fixture。
トークナイザは zenz と同じく1文字1トークン（バイト単位 BPE で、結合を1文字の中に限って学習させる）。
コーパスに無い字（転・機）はバイトのトークンに割れ、文字の途中で切れるトークンも試す。
"""

import json

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from core.constrained import ConstrainedZenz, LatticeMatcher, bounds_from_text  # noqa: E402
from core.kanakanji import Dictionary  # noqa: E402
from core.select import ZenzScorer, fluency  # noqa: E402
from eval.prompts import CONTEXT, INPUT, OUTPUT  # noqa: E402
from tests.test_core_constrained import ENTRIES, POS, READINGS  # noqa: E402

TOL = 1e-4
CORPUS = ["キョウハイイテンキデスネ", "今日は良い天気ですね", "京", "点", "木", "きょうはてんき", "ハ", "ネ",
          "。、.", INPUT + CONTEXT + OUTPUT, "abcdefghijklmnopqrstuvwxyz"]
LEFT_CONTEXTS = ["", "今日は良い天気ですね"]
FLUENCY_TEXTS = ["今日は良い天気ですね", "京は良い転機ですね", "キョウハテンキ", "点", "今日は、良い天気ですね。",
                 "a\"b\\c"]


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory) -> str:
    import torch
    from tokenizers import Regex, Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import GPT2Config, GPT2LMHeadModel

    path = tmp_path_factory.mktemp("zenz-like-char")
    alphabet = [c for c in pre_tokenizers.ByteLevel.alphabet() if c not in ("Ġ", "Ċ", "č")]
    tk = Tokenizer(models.BPE())
    # 学習のときだけ1文字ずつに割る（結合が文字をまたがない）。保存するのは GPT-2 と同じ ByteLevel
    tk.pre_tokenizer = pre_tokenizers.Sequence([pre_tokenizers.Split(Regex("."), "isolated"),
                                                pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False)])
    tk.decoder = decoders.ByteLevel()
    specials = ["[UNK]", "[PAD]", "<s>", "</s>"]
    tk.train_from_iterator(CORPUS * 20, trainers.BpeTrainer(vocab_size=1000, special_tokens=specials,
                                                            initial_alphabet=alphabet, show_progress=False))
    tk.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
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
def scorer(tiny_model, torch_device):
    return ZenzScorer(tiny_model, torch_device)


@pytest.fixture(scope="module")
def cz(scorer):
    return ConstrainedZenz(scorer)


@pytest.fixture(scope="module")
def dic():
    m = [[1000] * len(POS) for _ in POS]
    m[0][0] = 0
    return Dictionary.from_entries(ENTRIES, m, POS)


def test_tokenizer_is_char_level(scorer):
    tok = scorer.tok
    for ch in "今日は良い天気ですね":
        assert len(tok(ch, add_special_tokens=False).input_ids) == 1
    assert len(tok("転", add_special_tokens=False).input_ids) > 1  # コーパスに無い字はバイトに割れる
    assert len(tok("キョウ", add_special_tokens=False).input_ids) == 3


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_bounds_from_text_matches_decoded(cz, dic, left_context):
    # 候補ウィンドウ（core.window）は、生成していない文の語の切れ目を出力を辿り直して得る
    for r in READINGS:
        d = cz.decode_scored(r, dic, left_context)
        assert bounds_from_text(LatticeMatcher(r, dic), d.text) == d.bounds, d.text


@pytest.fixture(scope="module")
def ort(tiny_model, tmp_path_factory):
    from core.ort_backend import OrtBackend
    return OrtBackend(tiny_model, onnx_path=tmp_path_factory.mktemp("onnx") / "tiny.onnx", threads=1)


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_decode_batch_matches_torch(ort, cz, dic, left_context):
    hf = cz.decode_batch(READINGS, dic, left_context)
    ox = ort.decode_batch(READINGS, dic, left_context)
    for h, o in zip(hf, ox):
        assert (o.text, o.finished, o.bounds) == (h.text, h.finished, h.bounds)
        assert o.logp == pytest.approx(h.logp, abs=TOL)


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_fluency_matches_torch(ort, scorer, left_context):
    for h, o in zip(fluency(scorer, FLUENCY_TEXTS, left_context), ort.fluency(FLUENCY_TEXTS, left_context)):
        assert o == pytest.approx(h, abs=TOL)


def test_logprobs_per_sequence_eos_matches_torch(ort, scorer):
    # 候補ウィンドウ（core.window）は変換の確率（EOS あり）と自然さ（EOS なし）を 1 回で測る
    prompts = ["今日", "京", "キョウ", "点"] * 10  # 40 系列（1 回の束 32 を超える）。空のプロンプトは torch 版も扱わない
    conts = ["は良い天気ですね", "は", "キョウ", "木"] * 10
    eos = [True, False, True, False] * 10
    for h, o in zip(scorer.logprobs(prompts, conts, eos=eos), ort.logprobs(prompts, conts, eos=eos)):
        assert o == pytest.approx(h, abs=TOL)


def test_export_is_checked_and_atomic(tiny_model, tmp_path):
    from core.ort_backend import export_onnx
    out = tmp_path / "m.onnx"
    export_onnx(tiny_model, out)
    assert out.is_file() and sorted(x.name for x in tmp_path.iterdir()) == ["m.onnx"]


@pytest.fixture(scope="module")
def ort_plain(ort):
    # 共通の前置きを使い回さない同じセッション（使い回しても同じ量を返すことを確かめる）
    from core.ort_backend import OrtBackend
    x = OrtBackend.__new__(OrtBackend)
    x.__dict__.update(ort.__dict__)
    x.share, x.cache = False, type(ort.cache)()
    return x


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_shared_prefix_is_the_same_computation(ort, ort_plain, dic, left_context):
    ort.cache.clear()
    for _ in range(2):  # 2 回目は要求をまたいで取っておいた前置きを使う
        a = ort_plain.decode_batch(READINGS, dic, left_context)
        b = ort.decode_batch(READINGS, dic, left_context)
        for x, y in zip(a, b):
            assert (y.text, y.finished, y.bounds) == (x.text, x.finished, x.bounds)
            assert y.logp == pytest.approx(x.logp, abs=TOL)
        prompts = ["今日" + left_context] * 3 + ["京" + left_context] * 3 + ["点"]
        conts = ["は良い天気ですね", "は", "木", "は", "キョウ", "木", "木"]
        eos = [True, False] * 3 + [True]
        for x, y in zip(ort_plain.logprobs(prompts, conts, eos=eos), ort.logprobs(prompts, conts, eos=eos)):
            assert y == pytest.approx(x, abs=TOL)
    assert bool(ort.cache) == bool(left_context)  # 左文脈があれば前置きを取っておく（無ければ共通部が 1 トークンで使い回さない）


def test_window_alternatives_use_ort(ort, tiny_model, dic, torch_device, monkeypatch):
    pytest.importorskip("wordfreq")
    from core import window
    from core.pipeline import CorePipeline
    monkeypatch.setattr(Dictionary, "load", classmethod(lambda cls, *a, **k: dic))
    hf = CorePipeline(tiny_model, device=torch_device, k=3, punct="insert")
    ox = CorePipeline(k=3, punct="insert", backend=ort)
    assert window._scorer(ox) is ort
    for romaji in ["kyouhatenki", "kyou", "tenki"]:
        a, b = window.pool(hf, romaji, strict=True), window.pool(ox, romaji, strict=True)
        assert [e.text for e in b.entries] == [e.text for e in a.entries]
        for x, y in zip(a.entries, b.entries):
            assert y.score == pytest.approx(x.score, abs=1e-3)
        assert hf.convert_nbest(romaji) == ox.convert_nbest(romaji)


def test_lists_first_then_full_matches_nbest(ort, tiny_model, dic, monkeypatch):
    # 常駐入力（ime/server.py の Lists）: 最初の応答は 1 位と読みの候補だけ、後から別の書き方を足すと convert_nbest と同じ列
    pytest.importorskip("wordfreq")
    from core.pipeline import CorePipeline
    from ime.server import Lists
    monkeypatch.setattr(Dictionary, "load", classmethod(lambda cls, *a, **k: dic))
    ox = CorePipeline(k=3, punct="insert", backend=ort)
    for romaji in ["kyouhatenki", "kyou", "tenki"]:
        for lc in LEFT_CONTEXTS:
            lists = Lists(ox, "merge")
            first, full = lists.first(romaji, lc), lists.full(romaji, lc)
            assert full == ox.convert_nbest(romaji, lc)
            assert first and first[0] == full[0]
            assert Lists(ox, "merge").full(romaji, lc) == full   # 最初から full を頼んでも同じ
            assert Lists(ox, "reading").first(romaji, lc) == ox.convert_nbest(romaji, lc, order="reading")


def test_cancel_stops_ort_and_is_not_swallowed(ort, dic, monkeypatch):
    pytest.importorskip("wordfreq")
    from core import cancel, window
    from core.pipeline import CorePipeline
    with pytest.raises(cancel.Cancelled), cancel.scope(lambda: True):
        ort.decode_batch(READINGS, dic, "")
    monkeypatch.setattr(Dictionary, "load", classmethod(lambda cls, *a, **k: dic))
    ox = CorePipeline(k=3, punct="insert", backend=ort)
    for romaji in ["kyouhatenki", "kyou", "tenki"]:
        pl = window.pool(ox, romaji, alts=False)
        if len(window.pool(ox, romaji, alts=True).entries) == len(pl.entries):
            continue   # 別の書き方が無い（採点しない）
        with pytest.raises(cancel.Cancelled), cancel.scope(lambda: True):
            window.add_alternatives(ox, pl)   # 別の書き方の失敗を握りつぶす所に拾われない
        assert not pl.alts
        window.add_alternatives(ox, pl)
        assert pl.alts
        return
    pytest.skip("極小の辞書で別の書き方が出る打鍵列が無い")
