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


def _make_tiny(path, n_positions: int) -> str:
    import torch
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import GPT2Config, GPT2LMHeadModel

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
    cfg = GPT2Config(vocab_size=tk.get_vocab_size(), n_embd=64, n_layer=2, n_head=4, n_positions=n_positions,
                     bos_token_id=2, eos_token_id=3, pad_token_id=1)
    torch.manual_seed(0)
    GPT2LMHeadModel(cfg).save_pretrained(path)
    return str(path)


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory) -> str:
    return _make_tiny(tmp_path_factory.mktemp("zenz-like-constrained"), 256)


@pytest.fixture(scope="module")
def cz(tiny_model, torch_device):
    return ConstrainedZenz(ZenzScorer(tiny_model, torch_device))


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
    for b, s in zip(batch, single):
        assert b.text == s.text
        assert b.finished == s.finished
        assert b.bounds == s.bounds
        assert b.logp == pytest.approx(s.logp, abs=1e-3)


def test_outputs_stay_on_lattice(cz, dic):
    # 出力は辞書の語・かなのまま・記号の候補だけでできている（読みで説明できない表層は出ない）
    for d in cz.decode_batch(READINGS, dic):
        assert all(ch in "今日京は天気転機点木良いねキョウハテンキイイきょうはてんきい。." for ch in d.text), d.text


def test_bounds_are_word_boundaries(cz, dic):
    from core.constrained import LatticeMatcher, bounds_from_text

    for r, d in zip(READINGS, cz.decode_batch(READINGS, dic)):
        # 切れ目は 0 と末尾を除く昇順の文字位置で、出力を辿り直したものと一致する（トークン内部の切れ目も含む）
        assert list(d.bounds) == sorted(set(d.bounds))
        assert all(0 < b < len(d.text) for b in d.bounds)
        assert d.bounds == bounds_from_text(LatticeMatcher(r, dic), d.text)


def test_bounds_inside_multichar_token(dic):
    # 「今日は」が1トークンでも、今日|は の切れ目（文字位置 2）を落とさない
    from core.constrained import LatticeMatcher, bounds_from_text

    m = LatticeMatcher(_kana("キョウハ"), dic)
    assert 2 in bounds_from_text(m, "今日は")


def test_finished_rows_do_not_advance_position(tmp_path_factory, dic, torch_device):
    # 位置埋め込みが短いモデルで、長いプロンプトの系列が先に終わっても、残りの系列の生成で上限を超えない
    short = ConstrainedZenz(ZenzScorer(_make_tiny(tmp_path_factory.mktemp("short"), 40), torch_device))
    long_ctx = "今日は良い天気ですね" * 2  # プロンプトが長い系列（すぐ終わる短い読み）と、短い系列（長い読み）を混ぜる
    rs = [_kana("キ"), _kana("テンキハキョウハイイテンキ")]
    single = [short.decode_scored(rs[0], dic, long_ctx), short.decode_scored(rs[1], dic, "")]
    # バッチでは左文脈は共通なので、長い文脈で両方を回す（1件ずつも同じ条件で）
    single = [short.decode_scored(r, dic, long_ctx) for r in rs]
    batch = short.decode_batch(rs, dic, long_ctx)
    assert [d.text for d in batch] == [d.text for d in single]


def test_pick_respects_max_tries(tiny_model, dic, torch_device):
    import torch

    cz1 = ConstrainedZenz(ZenzScorer(tiny_model, torch_device), max_tries=1)
    m = __import__("core.constrained", fromlist=["LatticeMatcher"]).LatticeMatcher(_kana("キョウ"), dic)
    logits = torch.full((len(cz1.tok_bytes),), -10.0)
    bad = [t for t, b in enumerate(cz1.tok_bytes) if b == b"x"][0]  # ラティスに無いトークンを 1 位に
    good = [t for t, b in enumerate(cz1.tok_bytes) if b == "今".encode()][0]
    logits[bad], logits[good] = 1.0, 0.5
    assert cz1._pick(logits, m, m.start())[0] is None  # max_tries=1 では 1 位だけ見る
    assert ConstrainedZenz(ZenzScorer(tiny_model, torch_device), max_tries=2)._pick(logits, m, m.start())[0] == good


def test_batch_order_and_empty(cz, dic):
    assert cz.decode_batch([], dic) == []
    one = cz.decode_batch([READINGS[1]], dic)[0]
    assert one.text == cz.decode_scored(READINGS[1], dic).text
    # 並び順を変えても各読みの結果は変わらない
    rev = cz.decode_batch(READINGS[::-1], dic)
    fwd = cz.decode_batch(READINGS, dic)
    assert [d.text for d in rev] == [d.text for d in fwd][::-1]


SYM_READING = Reading((Seg("kana", 0, 3, "キョウ"), Seg("sym", 3, 4, ".", ("。", "."))))
EN_READINGS = [Reading((Seg("en", 0, 6, "github", ("GitHub", "github")),)),  # 登録語
               Reading((Seg("en", 0, 2, "jr", ("jr",)),)),  # 辞書に無い短い英字列
               Reading((Seg("en", 0, 4, "push", ("push",)),))]  # 一般語（wordfreq）


def test_punct_modes(cz, dic):
    ja = cz.decode_scored(SYM_READING, dic, punct="ja").text
    ascii_ = cz.decode_scored(SYM_READING, dic, punct="ascii").text
    assert ja.endswith("。") and ascii_.endswith(".")
    for mode in ("auto", "insert"):
        assert cz.decode_scored(SYM_READING, dic, punct=mode).text[-1] in "。."
    with pytest.raises(ValueError):
        cz.decode_scored(SYM_READING, dic, punct="full")


def test_punct_ja_keeps_symbols_without_fullwidth(cz, dic):
    # 全角候補の無い記号（@）は ja でも行き止まりにしない
    r = Reading((Seg("kana", 0, 3, "キョウ"), Seg("sym", 3, 4, "@", ("@",))))
    d = cz.decode_scored(r, dic, punct="ja")
    assert d.finished and d.text.endswith("@")


def test_en_case_modes_in_lattice(cz, dic):
    # ラティスでは zenz 以外のモードは打った綴りだけ（登録表記への置き換えは core.pipeline.render_en_case）
    for m in ("typed", "registered", "auto"):
        assert [cz.decode_scored(r, dic, en_case=m).text for r in EN_READINGS] == ["github", "jr", "push"]
    z = [cz.decode_scored(r, dic, en_case="zenz").text for r in EN_READINGS]
    assert z[0] in ("GitHub", "github") and z[1:] == ["jr", "push"]
    with pytest.raises(ValueError):
        cz.decode_scored(EN_READINGS[0], dic, en_case="upper")


def test_render_en_case():
    pytest.importorskip("wordfreq")
    from core.pipeline import render_en_case

    reading = Reading((Seg("en", 0, 6, "github", ("GitHub", "github")), Seg("kana", 6, 8, "ニ"),
                       Seg("en", 8, 12, "push", ("push",)), Seg("kana", 12, 20, "シテオイテ"),
                       Seg("en", 20, 22, "jr", ("jr",))))
    text = "githubにpushしておいてjr"
    assert render_en_case(text, reading, "typed") == text
    assert render_en_case(text, reading, "registered") == "GitHubにpushしておいてjr"
    assert render_en_case(text, reading, "auto") == "GitHubにpushしておいてJR"
    # 出力に綴りが見つからない区間（かなに変換された英語）は飛ばす
    assert render_en_case("ギットハブにpushしておいてjr", reading, "registered") == "ギットハブにpushしておいてjr"
    # 辞書語の表層（アイフォン→iPhone）の中の同じ綴りは拾わない: 語の切れ目に揃う出現だけを置き換える
    r2 = Reading((Seg("kana", 0, 5, "アイフォン"), Seg("en", 5, 6, "i", ("i",))))
    assert render_en_case("iPhonei", r2, "auto", bounds=(6,)) == "iPhoneI"


def test_decode_stops_when_cancelled(cz, dic):
    # 常駐入力の古い先読みは、生成の 1 手ごとに確かめてやめる（core.cancel）
    from core import cancel
    with pytest.raises(cancel.Cancelled), cancel.scope(lambda: True):
        cz.decode_batch([_kana("キョウ")], dic, "")
    with cancel.scope(lambda: False):
        assert cz.decode_batch([_kana("キョウ")], dic, "")
