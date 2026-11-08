import json

from tools import corpus as c


def test_apply_fixes_replaces_first_match_and_counts_misses():
    text, hit, miss = c.apply_fixes("資料を買えておいて。資料を買えて", [
        {"id": "x", "from": "資料を買えて", "to": "資料を変えて"},
        {"id": "x", "from": "存在しない", "to": "ほか"},
        {"id": "x", "from": "同じ", "to": "同じ"},
    ])
    assert text == "資料を変えておいて。資料を買えて"
    assert (hit, miss) == (1, 1)


def test_split_units_keeps_paragraphs_and_splits_long_ones():
    long_para = "\n".join(["あ" * 400, "い" * 400])
    units = c.split_units("一段落目\n\n二段落目\n続き\n\n" + long_para)
    assert units[:2] == ["一段落目", "二段落目\n続き"]
    assert units[2:] == ["あ" * 400, "い" * 400]


def _session(tmp_path, name, rows):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    with open(d / f"{name}.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session": name}) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_claude_archive_drops_automated_sessions_and_generated_text(tmp_path):
    _session(tmp_path, "auto", [{"ts": "2026-11-01T00:00:00Z", "role": "user", "text": "taskboxをdrainして。理由は…"}])
    _session(tmp_path, "mine", [
        {"ts": "2026-11-02T00:00:00Z", "role": "user", "text": "これを直しておいて"},
        {"ts": "2026-11-02T00:00:01Z", "role": "assistant", "text": "直しました"},
        {"ts": "2026-11-02T00:00:02Z", "role": "user", "text": "Base directory for this skill: なにか"},
        {"ts": "2026-11-02T00:00:03Z", "role": "user", "text": "貼るね <pasted_content id=\"a\">他人の文章</pasted_content id=\"a\">"},
        {"ts": "2026-11-02T00:00:04Z", "role": "user", "text": "ok"},
    ])
    got = c.collect_claude_archive({"path": str(tmp_path)})
    assert got == [("2026-11-02", "これを直しておいて"), ("2026-11-02", "貼るね")]


def test_dialogue_takes_only_the_labelled_speaker(tmp_path):
    p = tmp_path / "log.md"
    p.write_text("# 題\n\n**［ユーザー］**\n\n本人の発言\n\n**［Claude］**\n\nAI の発言\n\n**［ユーザー］**\n\n二つ目\n", encoding="utf-8")
    got = c.collect_dialogue({"path": str(p), "label": "ユーザー", "date": "2026-07-07"})
    assert got == [("2026-07-07", "本人の発言"), ("2026-07-07", "二つ目")]


def test_unit_id_is_stable_and_depends_on_source():
    assert c.unit_id("a", "文") == c.unit_id("a", "文")
    assert c.unit_id("a", "文") != c.unit_id("b", "文")
