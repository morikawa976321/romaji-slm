import json

from eval import parity


def _write(tmp_path, name, output, converter=None):
    path = tmp_path / f"{name}__gen_test.jsonl"
    row = {"id": "0", "category": "clean", "romaji": "x", "refs": ["例えば"], "output": output, "exact": False,
           "cands": [{"reading": "タトエバ", "edits": [], "text": "例えば", "finished": True, "lm": -1.0,
                      "fluency": -2.0, "read_cost": 0.0, "score": -3.0}]}
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    if converter:
        parity.env_path(path).write_text(json.dumps({"converter": converter}), encoding="utf-8")
    return path


def test_env_path_is_per_set():
    assert parity.env_path("O1__file_eval_sets_typo_hard.jsonl.jsonl").name == "O1__file_eval_sets_typo_hard.jsonl.env.json"
    assert parity.env_path("O1__ajimee.jsonl").name == "O1__ajimee.env.json"


def test_compare_flags_different_converters(tmp_path):
    a = _write(tmp_path, "A", "例えば", "core-ort:t=4")
    b = _write(tmp_path, "B", "例えば、", "core-ort:t=4,p=insert")
    res = parity.compare(a, b)
    assert res["diff"][0]["type"] == "punct"
    md = parity.render(res, "A", "B")
    assert "`core-ort:t=4` / B `core-ort:t=4,p=insert`" in md and "設定が違う" in md


def test_compare_without_env_files(tmp_path):
    a = _write(tmp_path, "A", "例えば")
    b = _write(tmp_path, "B", "例えば")
    md = parity.render(parity.compare(a, b), "A", "B")
    assert "記録なし" in md and "設定が違う" not in md
