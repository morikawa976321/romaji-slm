"""ONNX Runtime 版（core/ort_backend.py）が torch 版と同じ量を返すこと。極小モデルと辞書は tests/test_llama_backend.py と同じ。"""

from pathlib import Path

import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from core.select import fluency  # noqa: E402
from tests.test_core_constrained import READINGS  # noqa: E402
from tests.test_llama_backend import FLUENCY_TEXTS, LEFT_CONTEXTS, cz, dic, scorer, tiny_model  # noqa: E402,F401

TOL = 1e-4


@pytest.fixture(scope="module")
def ort(tiny_model, tmp_path_factory):  # noqa: F811
    from core.ort_backend import OrtBackend
    return OrtBackend(tiny_model, onnx_path=tmp_path_factory.mktemp("onnx") / "tiny.onnx", threads=1)


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_decode_batch_matches_torch(ort, cz, dic, left_context):  # noqa: F811
    hf = cz.decode_batch(READINGS, dic, left_context)
    ox = ort.decode_batch(READINGS, dic, left_context)
    for h, o in zip(hf, ox):
        assert (o.text, o.finished, o.bounds) == (h.text, h.finished, h.bounds)
        assert o.logp == pytest.approx(h.logp, abs=TOL)


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_fluency_matches_torch(ort, scorer, left_context):  # noqa: F811
    for h, o in zip(fluency(scorer, FLUENCY_TEXTS, left_context), ort.fluency(FLUENCY_TEXTS, left_context)):
        assert o == pytest.approx(h, abs=TOL)


def test_logprobs_per_sequence_eos_matches_torch(ort, scorer):  # noqa: F811
    # 候補ウィンドウ（core.window）は変換の確率（EOS あり）と自然さ（EOS なし）を 1 回で測る
    prompts = ["今日", "京", "キョウ", "点"] * 10  # 40 系列（1 回の束 32 を超える）。空のプロンプトは torch 版も扱わない
    conts = ["は良い天気ですね", "は", "キョウ", "木"] * 10
    eos = [True, False, True, False] * 10
    for h, o in zip(scorer.logprobs(prompts, conts, eos=eos), ort.logprobs(prompts, conts, eos=eos)):
        assert o == pytest.approx(h, abs=TOL)


def test_export_is_checked_and_atomic(tiny_model, tmp_path):  # noqa: F811
    from core.ort_backend import export_onnx
    out = tmp_path / "m.onnx"
    export_onnx(tiny_model, out)
    assert out.is_file() and not (tmp_path / "m.onnx.tmp").exists()


def test_window_alternatives_use_ort(ort, tiny_model, dic, monkeypatch):  # noqa: F811
    pytest.importorskip("wordfreq")
    from core import window
    from core.kanakanji import Dictionary
    from core.pipeline import CorePipeline
    monkeypatch.setattr(Dictionary, "load", classmethod(lambda cls, *a, **k: dic))
    hf = CorePipeline(tiny_model, k=3, punct="insert")
    ox = CorePipeline(k=3, punct="insert", backend=ort)
    assert window._can_score(ox) and window._scorer(ox) is ort
    for romaji in ["kyouhatenki", "kyou", "tenki"]:
        a, b = window.pool(hf, romaji, strict=True), window.pool(ox, romaji, strict=True)
        assert [e.text for e in b.entries] == [e.text for e in a.entries]
        for x, y in zip(a.entries, b.entries):
            assert y.score == pytest.approx(x.score, abs=1e-3)
        assert hf.convert_nbest(romaji) == ox.convert_nbest(romaji)
