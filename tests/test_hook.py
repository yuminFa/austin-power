import io
import json
import subprocess
import sys

import pytest

from austin_power import db, hook


def run(event, payload, tmp_path, monkeypatch, raw=None):
    env = {"AUSTIN_POWER_HOME": str(tmp_path / "h"), "AUSTIN_POWER_PORT": "1", "AUSTIN_POWER_PROJECT": "proj"}
    out, err = io.StringIO(), io.StringIO()
    code = hook.main(event, stdin=io.StringIO(raw if raw is not None else json.dumps(payload)), stdout=out, stderr=err, env=env)
    return code, out.getvalue(), err.getvalue()

def test_import_is_light():
    code = "import sys, austin_power.hook; bad=[m for m in ('kiwipiepy','apsw','mcp','uvicorn','anyio') if m in sys.modules]; print(bad)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.stdout.strip() == "[]"

@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"cwd": 3}'])
def test_malformed_stdin_is_fail_open(raw, tmp_path, monkeypatch):
    for ev in ("post-compact", "session-start"):
        code, out, _ = run(ev, None, tmp_path, monkeypatch, raw=raw)
        assert code == 0 and out == ""

def test_post_compact_empty_summary_noop(tmp_path, monkeypatch):
    code, _out, _ = run("post-compact", {"session_id": "s", "cwd": str(tmp_path), "compact_summary": ""}, tmp_path, monkeypatch)
    assert code == 0 and not (tmp_path / "h" / "memory.db").exists()

@pytest.mark.kiwi
def test_post_compact_fallback_writes_and_upserts(tmp_path, monkeypatch):
    p = {"session_id": "abc", "cwd": str(tmp_path), "trigger": "auto", "compact_summary": "첫 요약"}
    assert run("post-compact", p, tmp_path, monkeypatch)[0] == 0   # port 1 → refused → fallback
    p["compact_summary"] = "두 번째 요약"
    assert run("post-compact", p, tmp_path, monkeypatch)[0] == 0
    conn = db.open_db(tmp_path / "h" / "memory.db")
    assert conn.execute("select count(*), title, kind, project, body from note").fetchone() == (1, "session abc", "session", "proj", "두 번째 요약")

@pytest.mark.kiwi
def test_post_compact_no_fallback_when_server_lock_held(tmp_path, monkeypatch):
    lock = db.ServerLock(tmp_path / "h" / "server.lock"); assert lock.acquire()
    try:
        code, _, err = run("post-compact", {"session_id": "s", "cwd": str(tmp_path), "compact_summary": "x"}, tmp_path, monkeypatch)
        assert code == 0 and "unreachable" in err
        assert not (tmp_path / "h" / "memory.db").exists()
    finally:
        lock.release()

def test_session_start_compact_source_silent(tmp_path, monkeypatch):
    code, out, _ = run("session-start", {"session_id": "s", "cwd": str(tmp_path), "source": "compact"}, tmp_path, monkeypatch)
    assert code == 0 and out == ""

def test_session_start_server_down_silent(tmp_path, monkeypatch):
    code, out, _ = run("session-start", {"session_id": "s", "cwd": str(tmp_path), "source": "startup"}, tmp_path, monkeypatch)
    assert code == 0 and out == ""

def test_format_context_respects_cap():
    items = [{"id": i, "kind": "fact", "title": f"t{i}", "preview": "가" * 150, "updated_at": "2026-09-23T00:00:00Z"} for i in range(20)]
    text = hook.format_context("proj", items, cap=1000)
    assert len(text) <= 1000 and text.startswith("austin-power: recent memories for proj")
    assert "- [fact] t0 (#0, 2026-09-23): " in text

def test_resolve_project(tmp_path):
    assert hook.resolve_project(str(tmp_path), {"AUSTIN_POWER_PROJECT": "x"}) == "x"
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    (tmp_path / "repo" / "sub").mkdir()
    assert hook.resolve_project(str(tmp_path / "repo" / "sub"), {}) == "repo"
    assert hook.resolve_project(str(tmp_path), {}) == tmp_path.name
    assert hook.resolve_project("", {}) == ""
