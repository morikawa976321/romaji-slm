import pytest

from eval.prompts import CONTEXT, INPUT, OUTPUT, TEMPLATES, build_prompt, normalize_like_romaji2ja


def test_special_chars():
    assert (INPUT, OUTPUT, CONTEXT) == ("", "", "")


def test_romaji2ja():
    assert build_prompt("romaji2ja", "kyou,ha", "無視される") == "kyou,ha"
    assert build_prompt("romaji2ja-normalized", "Kyou, ha.") == "kyouha"
    assert normalize_like_romaji2ja("a-b c/d") == "abcd"


def test_zenz_v3_puts_context_first_and_kana_input():
    p = build_prompt("zenz-v3", "kyouha", "私の名前は")
    assert p == "私の名前はキョウハ"


def test_zenz_v3_without_context_omits_context_marker():
    assert build_prompt("zenz-v3", "kyou") == "キョウ"


def test_zenz_v3_romaji_keeps_romaji():
    assert build_prompt("zenz-v3-romaji", "github,push", "ctx") == "ctxgithub,push"
    assert build_prompt("zenz-v3-romaji", "abc") == "abc"


def test_unknown_template():
    with pytest.raises(ValueError):
        build_prompt("nope", "a")


def test_all_templates_documented():
    assert all(t.description for t in TEMPLATES.values())
