import importlib.util
import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "agentmemory"
spec = importlib.util.spec_from_file_location("am", Path(__file__).parents[1] / "contrib" / "agentmemory_to_jsonl.py")
am = importlib.util.module_from_spec(spec); spec.loader.exec_module(am)

def test_convert():
    rows, skips = am.convert(FIX)
    by = {(r["project"], r["title"]): r for r in rows}
    assert len(rows) == 5 and any("mem_5" in s for s in skips)
    q = by[("shop", "견적 시스템")]
    assert q["kind"] == "architecture" and q["body"].endswith("\n\nconcepts: quote, 견적\nfiles: a.py")
    assert by[("web", "배포 절차")]["updated_at"] == "2026-07-14T00:00:00Z"   # most frequent project web(2) over api(1)
    assert ("api", "배포 절차") in by                                         # mem_3 → api, no collision
    assert all(r["title"] != "old" for r in rows)


def test_convert_summaries():
    rows, skips = am.convert(FIX)
    by = {(r["project"], r["title"]): r for r in rows}
    assert any("skip summary sum_3" in s for s in skips)

    full = by[("shop", "session sess_1")]
    assert full["kind"] == "session"
    assert full["session_id"] == "sess_1"
    assert full["created_at"] == full["updated_at"] == "2026-07-20T00:00:00Z"
    assert full["body"] == (
        "견적 시스템 세션 요약\n\n세션 내러티브 본문"
        "\n\nkey decisions:\n- 결정 a\n- 결정 b"
        "\n\nfiles: a.py, b.py"
        "\n\nconcepts: quote, 견적"
    )

    json_str = by[("web", "session sess_2")]
    assert json_str["kind"] == "session"
    assert json_str["session_id"] == "sess_2"
    assert json_str["body"] == (
        "배포 세션 요약\n\n배포 내러티브"
        "\n\nkey decisions:\n- decision x"
        "\n\nfiles: deploy.sh"
        "\n\nconcepts: deploy"
    )

    assert ("web", "session sum_3") not in by and not any(
        r["session_id"] == "" for r in rows if r["kind"] == "session"
    )


def test_main_reports_memories_and_summaries_counts(capsys):
    am.main([str(FIX), "-o", "/dev/null"])
    err = capsys.readouterr().err
    assert "converted 3 memories, 2 summaries, skipped" in err


def test_main_no_summaries_flag(capsys):
    am.main([str(FIX), "-o", "/dev/null", "--no-summaries"])
    err = capsys.readouterr().err
    assert "converted 3 memories, 0 summaries, skipped" in err


def test_no_summaries_flag_excludes_sessions(tmp_path):
    (tmp_path / "mem%3Amemories.bin").write_text("{}")
    (tmp_path / "mem%3Asummaries.bin").write_text(
        json.dumps({"s1": {"sessionId": "sess_x", "project": "p", "title": "t",
                            "narrative": "n", "createdAt": "2026-01-01T00:00:00Z"}})
    )
    rows, skips = am.convert(tmp_path, include_summaries=False)
    assert rows == [] and skips == []
    rows, skips = am.convert(tmp_path)
    assert len(rows) == 1 and rows[0]["kind"] == "session"

def test_collision_tracks_every_emitted_title(tmp_path):
    mems = {
        "m1": {"id": "m1", "title": "foo", "content": "c1", "type": "fact", "isLatest": True,
               "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"},
        "m2": {"id": "m2", "title": "foo", "content": "c2", "type": "fact", "isLatest": True,
               "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"},
        "m3": {"id": "m3", "title": "foo (2)", "content": "c3", "type": "fact", "isLatest": True,
               "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"},
    }
    (tmp_path / "mem%3Amemories.bin").write_text(json.dumps(mems))
    rows, skips = am.convert(tmp_path)
    assert not skips
    titles = [r["title"] for r in rows]
    assert len(titles) == len(set(titles)) == 3
    assert all(len(t) <= 200 for t in titles)


def test_collision_suffix_and_limits(tmp_path):
    mems = {f"m{i}": {"id": f"m{i}", "title": "x" * 250, "content": "c", "type": "Weird Type!", "isLatest": True,
                      "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"} for i in range(2)}
    (tmp_path / "mem%3Amemories.bin").write_text(json.dumps(mems))
    rows, _ = am.convert(tmp_path)
    titles = [r["title"] for r in rows]  # row order follows sorted memory ids (m0, m1)
    assert len(titles[0]) == 200 and titles[1].endswith(" (2)") and len(titles[1]) == 200
    assert rows[0]["kind"] == "weird-type"
