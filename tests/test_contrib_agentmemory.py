import importlib.util
import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "agentmemory"
spec = importlib.util.spec_from_file_location("am", Path(__file__).parents[1] / "contrib" / "agentmemory_to_jsonl.py")
am = importlib.util.module_from_spec(spec); spec.loader.exec_module(am)

def test_convert():
    rows, skips = am.convert(FIX)
    by = {(r["project"], r["title"]): r for r in rows}
    assert len(rows) == 3 and any("mem_5" in s for s in skips)
    q = by[("shop", "견적 시스템")]
    assert q["kind"] == "architecture" and q["body"].endswith("\n\nconcepts: quote, 견적\nfiles: a.py")
    assert by[("web", "배포 절차")]["updated_at"] == "2026-07-14T00:00:00Z"   # most frequent project web(2) over api(1)
    assert ("api", "배포 절차") in by                                         # mem_3 → api, no collision
    assert all(r["title"] != "old" for r in rows)

def test_collision_suffix_and_limits(tmp_path):
    mems = {f"m{i}": {"id": f"m{i}", "title": "x" * 250, "content": "c", "type": "Weird Type!", "isLatest": True,
                      "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"} for i in range(2)}
    (tmp_path / "mem%3Amemories.bin").write_text(json.dumps(mems))
    rows, _ = am.convert(tmp_path)
    titles = [r["title"] for r in rows]  # row order follows sorted memory ids (m0, m1)
    assert len(titles[0]) == 200 and titles[1].endswith(" (2)") and len(titles[1]) == 200
    assert rows[0]["kind"] == "weird-type"
