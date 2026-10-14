"""llama-server の実行系（core/llama_backend.py・core/gbnf.py）が transformers 版と同じ量を返すこと。

重みはランダム初期化の極小 GPT-2、辞書は tests/test_core_constrained.py の手書きの fixture。
トークナイザは zenz と同じく1文字1トークン（バイト単位 BPE で、結合を1文字の中に限って学習させる）。
コーパスに無い字（転・機）はバイトのトークンに割れ、文字の途中で切れるトークンも試す。
llama.cpp を使うテストは、環境変数 LLAMA_CPP_DIR がビルド済みのチェックアウトを指すときだけ動く。
"""

import json
import os
import random
import re
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

from core.constrained import ConstrainedZenz, LatticeMatcher  # noqa: E402
from core.gbnf import bounds_from_text, grammar_size, lattice_grammar, matcher_grammar  # noqa: E402
from core.kanakanji import Dictionary  # noqa: E402
from core.select import ZenzScorer, fluency  # noqa: E402
from core.types import Reading, Seg  # noqa: E402
from eval.prompts import CONTEXT, INPUT, OUTPUT  # noqa: E402
from tests.test_core_constrained import ENTRIES, POS, READINGS, SYM_READING  # noqa: E402

CORPUS = ["キョウハイイテンキデスネ", "今日は良い天気ですね", "京", "点", "木", "きょうはてんき", "ハ", "ネ",
          "。、.", INPUT + CONTEXT + OUTPUT, "abcdefghijklmnopqrstuvwxyz"]
LEFT_CONTEXTS = ["", "今日は良い天気ですね"]


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
def scorer(tiny_model):
    return ZenzScorer(tiny_model, "cpu")


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


# ---- 文法（llama.cpp 不要） ----

_TOKEN = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|([A-Za-z0-9-]+))')


def _unescape(s: str) -> str:
    return re.sub(r"\\(x[0-9A-Fa-f]{2}|.)", lambda m: chr(int(m[1][1:], 16)) if m[1][0] == "x" else m[1], s)


def parse_grammar(g: str) -> dict[str, list[tuple[str, str | None]]]:
    """core.gbnf が書く形（`名前 ::= "字" 名前 | "字" | ...`）だけを読む小さな解析器。"""
    rules = {}
    for line in g.strip().splitlines():
        name, _, body = line.partition(" ::= ")
        alts = []
        for alt in re.split(r'\s\|\s(?=(?:[^"]*"[^"]*")*[^"]*$)', body):
            lit, ref, pos = "", None, 0
            while pos < len(alt):
                m = _TOKEN.match(alt, pos)
                assert m and m.end() > pos, (line, alt[pos:])
                if m[1] is not None:
                    assert ref is None
                    lit += _unescape(m[1])
                else:
                    ref = m[2]
                pos = m.end()
            alts.append((lit, ref))
        rules[name] = alts
    return rules


def accepts(rules: dict, text: str) -> bool:
    memo: dict[tuple[str, int], bool] = {}

    def go(rule: str, i: int) -> bool:
        if (rule, i) not in memo:
            memo[rule, i] = any(text.startswith(lit, i)
                                and (go(ref, i + len(lit)) if ref else i + len(lit) == len(text))
                                for lit, ref in rules[rule])
        return memo[rule, i]
    return go("root", 0)


def lattice_paths(m: LatticeMatcher) -> set[str]:
    out = set()

    def walk(node: int, acc: bytes):
        if node == m.final:
            out.add(acc.decode())
        for b, e in m.out.get(node, ()):
            walk(e, acc + b)
    walk(0, b"")
    return out


def _validator() -> Path | None:
    d = os.environ.get("LLAMA_CPP_DIR")
    for name in ("llama-gbnf-validator", "test-gbnf-validator"):
        if d and (Path(d) / "build" / "bin" / name).is_file():
            return Path(d) / "build" / "bin" / name
    return None


GRAMMAR_READINGS = READINGS + [
    Reading((Seg("kana", 0, 3, "キョウ"), Seg("sym", 3, 4, "\"", ('"', "”")), Seg("kana", 4, 6, "テンキ"))),
    Reading((Seg("en", 0, 6, "github", ("GitHub", "github")), Seg("sym", 6, 7, "\\", ("\\", "＼")))),
]


@pytest.mark.parametrize("i", range(len(GRAMMAR_READINGS)))
def test_grammar_accepts_exactly_lattice_paths(dic, i, tmp_path):
    reading = GRAMMAR_READINGS[i]
    m = LatticeMatcher(reading, dic)
    g = matcher_grammar(m)
    assert g == lattice_grammar(reading, dic)
    rules = parse_grammar(g)
    paths = lattice_paths(m)
    assert paths and all(accepts(rules, p) for p in paths)
    rng = random.Random(i)
    chars = sorted({c for p in paths for c in p}) + ["x", "転"]
    negatives = [p + c for p in list(paths)[:20] for c in ("。", "木")] + [p[:-1] for p in paths if len(p) > 1]
    negatives += ["".join(rng.choice(chars) for _ in range(rng.randint(1, 12))) for _ in range(300)]
    for t in negatives:
        assert accepts(rules, t) == (t in paths), t
    nrules, nbytes = grammar_size(g)
    assert nrules == len(rules) and nbytes == len(g.encode())
    exe = _validator()
    if exe is None:
        return
    (tmp_path / "g.gbnf").write_text(g, encoding="utf-8")
    for t in sorted(paths)[:30] + negatives[:30]:
        (tmp_path / "in.txt").write_text(t, encoding="utf-8")
        r = subprocess.run([str(exe), str(tmp_path / "g.gbnf"), str(tmp_path / "in.txt")],
                           capture_output=True, timeout=30)
        # 不受理のときは入力を色付きで表示し、バイトの途中で色を変えるので UTF-8 として読めない
        assert (b"Input string is valid" in r.stdout) == (t in paths), (t, r.stdout)


def test_grammar_prunes_dead_ends(dic):
    m = LatticeMatcher(READINGS[2], dic)
    g = matcher_grammar(m)
    live = {int(n[1:]) for n in parse_grammar(g) if n != "root"}
    for n in live:
        assert any(e == m.final or e in live for _, e in m.out[n])


@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_bounds_from_text_matches_decoded(cz, dic, left_context):
    for r in READINGS:
        d = cz.decode_scored(r, dic, left_context)
        assert bounds_from_text(LatticeMatcher(r, dic), d.text) == d.bounds, d.text


# ---- llama-server との同等性 ----

LLAMA_CPP_DIR = os.environ.get("LLAMA_CPP_DIR")
needs_llama = pytest.mark.skipif(
    not (LLAMA_CPP_DIR and (Path(LLAMA_CPP_DIR) / "convert_hf_to_gguf.py").is_file()),
    reason="LLAMA_CPP_DIR（llama.cpp のチェックアウトとビルド）がない")


@pytest.fixture(scope="module")
def backend(tiny_model, tmp_path_factory):
    from core.llama_backend import LlamaBackend
    from infer import to_gguf
    gguf = tmp_path_factory.mktemp("gguf") / "tiny-F32.gguf"
    to_gguf.convert(Path(tiny_model), gguf, "f32", Path(LLAMA_CPP_DIR))
    b = LlamaBackend(gguf, n_slots=5, bin_dir=Path(LLAMA_CPP_DIR) / "build" / "bin")
    yield b
    b.close()


TOL = 1e-2


@needs_llama
@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_decode_batch_matches_hf(backend, cz, dic, left_context):
    hf = cz.decode_batch(READINGS, dic, left_context)
    ll = backend.decode_batch(READINGS, dic, left_context)
    assert len(ll) == len(hf)
    n_same = 0
    for r, h, g in zip(READINGS, hf, ll):
        if not h.finished:
            # HF 版は行き止まりの節点に入ると候補が尽きて止まる。文法は行き止まりを刈るので、llama 版は終点まで行ける
            assert g.text in lattice_paths(LatticeMatcher(r, dic)) or not g.finished
            continue
        n_same += 1
        assert (g.text, g.finished, g.bounds) == (h.text, h.finished, h.bounds)
        assert g.logp == pytest.approx(h.logp, abs=TOL)
    assert n_same >= 4
    # ランダムな重みは字をバイトのトークンで出しがちで、載らない logprob の取り直しも通る
    assert backend.n_refill > 0
    assert backend.eos_logprob_included is True  # b11562 以降は EOS の logprob も載る


@needs_llama
@pytest.mark.parametrize("punct", ["ja", "ascii"])
def test_decode_modes_match_hf(backend, cz, dic, punct):
    h = cz.decode_scored(SYM_READING, dic, punct=punct)
    g = backend.decode_batch([SYM_READING], dic, punct=punct)[0]
    assert (g.text, g.finished) == (h.text, h.finished)
    assert g.logp == pytest.approx(h.logp, abs=TOL)


FLUENCY_TEXTS = ["今日は良い天気ですね", "京は良い転機ですね", "キョウハテンキ", "点", "今日は、良い天気ですね。",
                 "a\"b\\c"]


@needs_llama
@pytest.mark.parametrize("left_context", LEFT_CONTEXTS)
def test_fluency_matches_hf(backend, scorer, left_context):
    # 強制生成のトークン分割が HF のトークナイザと同じになる文だけ比べられる（1文字1トークンの字と、
    # 語彙に無い字のバイト列。後者は途中までのバイトのトークンもありうるので、その場合は分割が変わりうる）
    hf = fluency(scorer, FLUENCY_TEXTS, left_context)
    ll = backend.fluency(FLUENCY_TEXTS, left_context)
    for t, h, g in zip(FLUENCY_TEXTS, hf, ll):
        assert g == pytest.approx(h, abs=TOL), t
    # 語彙に無い字（転・機）を含む文は、強制生成の分割が正規の分割と変わり、位置ごとの取り直しを通る
    assert backend.n_resplit > 0


@needs_llama
def test_pipeline_matches_hf(backend, tiny_model, dic, monkeypatch):
    pytest.importorskip("wordfreq")
    from core.pipeline import CorePipeline
    monkeypatch.setattr(Dictionary, "load", classmethod(lambda cls, *a, **k: dic))
    hf = CorePipeline(tiny_model, k=3, punct="insert")
    ll = CorePipeline(k=3, punct="insert", backend=backend)
    for romaji in ["kyouhatenki", "kyou", "tenki"]:
        ch, cl = hf.candidates(romaji), ll.candidates(romaji)
        assert [c["text"] for c in cl] == [c["text"] for c in ch]
        for a, b in zip(ch, cl):
            assert b["score"] == pytest.approx(a["score"], abs=3 * TOL)
            assert b["bounds"] == a["bounds"]
        assert ll.convert(romaji) == hf.convert(romaji)
