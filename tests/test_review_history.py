import datetime as dt
import importlib
import json


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMAJI_SLM_DATA", str(tmp_path))
    import tools.review_history as rh
    return importlib.reload(rh)


def write_history(path, times):
    path.write_text("".join(json.dumps({"time": t, "event": "commit", "romaji": t, "text": t}) + "\n" for t in times),
                    encoding="utf-8")


def test_mark_removes_reviewed_rows_and_work(tmp_path, monkeypatch, capsys):
    rh = load(tmp_path, monkeypatch)
    write_history(rh.HISTORY, ["2026-10-16T10:00", "2026-10-16T11:00", "2026-10-16T12:00"])
    (rh.REVIEW / "2026-10-16").mkdir(parents=True)
    (rh.REVIEW / "2026-10-16" / "new.jsonl").write_text("x", encoding="utf-8")
    old = (dt.date.today() - dt.timedelta(days=rh.REAL_KEEP_DAYS + 1)).isoformat()
    (rh.REVIEW / "real_local.jsonl").write_text(
        json.dumps({"romaji": "a", "meta": {"source": old}}) + "\n" +
        json.dumps({"romaji": "b", "meta": {"source": dt.date.today().isoformat()}}) + "\n", encoding="utf-8")
    assert rh.main(["mark", "2026-10-16T11:00"]) == 0
    left = [json.loads(line)["time"] for line in rh.HISTORY.read_text(encoding="utf-8").splitlines()]
    assert left == ["2026-10-16T12:00"]
    assert not (rh.REVIEW / "2026-10-16").exists()
    assert [json.loads(line)["romaji"] for line in (rh.REVIEW / "real_local.jsonl").read_text(encoding="utf-8").splitlines()] == ["b"]
    assert json.loads(rh.STATE.read_text(encoding="utf-8"))["reviewed_until"] == "2026-10-16T11:00"
    assert rh.main(["mark", "2026-10-16T10:30"]) == 1   # マーカーは戻さない


def test_count_and_extract_after_marker(tmp_path, monkeypatch, capsys):
    rh = load(tmp_path, monkeypatch)
    write_history(rh.HISTORY, ["2026-10-16T10:00", "2026-10-16T09:00"])
    rh.main(["count"])
    assert capsys.readouterr().out.strip() == "2"
    rh.main(["extract"])
    info = json.loads(capsys.readouterr().out)
    assert info["rows"] == 2 and info["until"] == "2026-10-16T10:00"
    rows = [json.loads(line) for line in (rh.REVIEW / dt.date.today().isoformat() / "new.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["time"] for r in rows] == ["2026-10-16T09:00", "2026-10-16T10:00"]   # 時刻の順に並べ直す
