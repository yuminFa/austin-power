import io
import json
import subprocess
import sys
import urllib.request

import pytest

from austin_power import db, hook


def run(event, payload, tmp_path, monkeypatch, raw=None, extra_env=None):
    env = {"AUSTIN_POWER_HOME": str(tmp_path / "h"), "AUSTIN_POWER_PORT": "1", "AUSTIN_POWER_PROJECT": "proj"}
    env.update(extra_env or {})
    out, err = io.StringIO(), io.StringIO()
    code = hook.main(event, stdin=io.StringIO(raw if raw is not None else json.dumps(payload)), stdout=out, stderr=err, env=env)
    return code, out.getvalue(), err.getvalue()


class _FakePopen:
    """Captures Popen() calls instead of spawning anything (C1-C6)."""

    calls: list = []  # noqa: RUF012 - reset per-test by the fake_popen fixture, not a shared default

    def __init__(self, argv, **kwargs):
        type(self).calls.append({"argv": argv, **kwargs})

    def poll(self):
        return None


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen.calls

def test_import_is_light():
    code = ("import sys, austin_power.hook, austin_power.extract; "
            "bad=[m for m in ('kiwipiepy','apsw','mcp','uvicorn','anyio') if m in sys.modules]; print(bad)")
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
def test_post_compact_fallback_creates_private_home(tmp_path, monkeypatch):
    p = {"session_id": "abc", "cwd": str(tmp_path), "compact_summary": "요약"}
    assert run("post-compact", p, tmp_path, monkeypatch)[0] == 0
    home = tmp_path / "h"
    assert home.is_dir()
    assert (home.stat().st_mode & 0o777) == 0o700
    # the db file itself doesn't need its own restrictive mode: a 0700 parent
    # already blocks group/other from traversing into the directory at all.
    assert (home / "memory.db").exists()

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

def test_call_tool_ignores_env_proxy(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"result": {"structuredContent": {"ok": True}}}).encode()

    class FakeOpener:
        def open(self, req, timeout=None):
            captured["opened_with_timeout"] = timeout
            return FakeResp()

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)

    cfg = type("Cfg", (), {"mcp_url": "http://127.0.0.1:7760/mcp"})()
    result = hook.call_tool(cfg, "tok", "recent", {}, timeout=3.0)

    assert result == {"ok": True}
    assert captured["opened_with_timeout"] == 3.0
    assert len(captured["handlers"]) == 1
    h = captured["handlers"][0]
    assert isinstance(h, urllib.request.ProxyHandler)
    assert h.proxies == {}

def _job_files(tmp_path):
    jobs = tmp_path / "h" / "jobs"
    return sorted(jobs.glob("*.json")) if jobs.is_dir() else []

def test_extract_mode_defaults_and_invalid():
    assert hook.extract_mode({}) == "auto"
    assert hook.extract_mode({"AUSTIN_POWER_EXTRACT": "codex"}) == "codex"
    assert hook.extract_mode({"AUSTIN_POWER_EXTRACT": "claude"}) == "claude"
    assert hook.extract_mode({"AUSTIN_POWER_EXTRACT": "off"}) == "off"
    assert hook.extract_mode({"AUSTIN_POWER_EXTRACT": "bogus"}) == "auto"

# C1: PostCompact normal -> summary saved + job spawned, hook returns without waiting.
@pytest.mark.kiwi
def test_post_compact_spawns_extract_job(tmp_path, monkeypatch, fake_popen):
    p = {"session_id": "abc", "cwd": str(tmp_path), "compact_summary": "요약 " * 5}
    code, _out, _err = run("post-compact", p, tmp_path, monkeypatch)
    assert code == 0
    assert len(fake_popen) == 1
    call = fake_popen[0]
    assert call["argv"] == [sys.executable, "-m", "austin_power.extract", call["argv"][3]]
    assert call["start_new_session"] is True
    assert call["stdin"] == subprocess.DEVNULL and call["stdout"] == subprocess.DEVNULL and call["stderr"] == subprocess.DEVNULL
    assert call["close_fds"] is True
    assert call["env"]["AUSTIN_POWER_EXTRACTOR_CHILD"] == "1"
    jobs = _job_files(tmp_path)
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text())
    assert job == {"source": "compact", "session_id": "abc", "cwd": str(tmp_path), "text": p["compact_summary"]}
    import os as _os
    if _os.name == "posix":
        assert (jobs[0].stat().st_mode & 0o777) == 0o600
        assert (jobs[0].parent.stat().st_mode & 0o777) == 0o700

# C1b: the untruncated original summary is spawned even when the saved copy is truncated.
@pytest.mark.kiwi
def test_post_compact_spawns_original_untruncated_summary(tmp_path, monkeypatch, fake_popen):
    big = "가" * (hook.SUMMARY_MAX + 500)
    p = {"session_id": "abc", "cwd": str(tmp_path), "compact_summary": big}
    run("post-compact", p, tmp_path, monkeypatch)
    job = json.loads(_job_files(tmp_path)[0].read_text())
    assert job["text"] == big
    conn = db.open_db(tmp_path / "h" / "memory.db")
    saved_body = conn.execute("select body from note").fetchone()[0]
    assert len(saved_body) < len(big)

# C2: PostCompact still spawns when the server is down (summary falls back to local save).
@pytest.mark.kiwi
def test_post_compact_spawns_extract_job_when_server_down(tmp_path, monkeypatch, fake_popen):
    p = {"session_id": "abc", "cwd": str(tmp_path), "compact_summary": "x" * 250}
    run("post-compact", p, tmp_path, monkeypatch)
    assert len(fake_popen) == 1
    assert (tmp_path / "h" / "memory.db").exists()

# C3: AUSTIN_POWER_EXTRACT=off -> no spawn for either event.
def test_extract_off_skips_spawn_post_compact(tmp_path, monkeypatch, fake_popen):
    p = {"session_id": "abc", "cwd": str(tmp_path), "compact_summary": ""}
    run("post-compact", p, tmp_path, monkeypatch, extra_env={"AUSTIN_POWER_EXTRACT": "off"})
    assert fake_popen == [] and _job_files(tmp_path) == []

def test_extract_off_skips_spawn_session_end(tmp_path, monkeypatch, fake_popen):
    p = {"session_id": "abc", "cwd": str(tmp_path), "transcript_path": str(tmp_path / "t.jsonl")}
    run("session-end", p, tmp_path, monkeypatch, extra_env={"AUSTIN_POWER_EXTRACT": "off"})
    assert fake_popen == [] and _job_files(tmp_path) == []

# C4: SessionEnd normal -> job with transcript_path spawned, hook does no parsing itself.
def test_session_end_spawns_extract_job(tmp_path, monkeypatch, fake_popen):
    transcript = tmp_path / "does-not-exist.jsonl"
    p = {"session_id": "abc", "cwd": str(tmp_path), "transcript_path": str(transcript), "reason": "clear"}
    code, out, _err = run("session-end", p, tmp_path, monkeypatch)
    assert code == 0 and out == ""
    assert len(fake_popen) == 1
    job = json.loads(_job_files(tmp_path)[0].read_text())
    assert job == {"source": "session-end", "session_id": "abc", "cwd": str(tmp_path), "transcript_path": str(transcript)}

# C5: SessionEnd with missing session_id/transcript_path -> no-op, no spawn.
@pytest.mark.parametrize("payload", [
    {"cwd": "x", "transcript_path": "t"},
    {"session_id": "", "cwd": "x", "transcript_path": "t"},
    {"session_id": "s", "cwd": "x"},
    {"session_id": "s", "cwd": "x", "transcript_path": ""},
    {"session_id": "s", "cwd": "x", "transcript_path": 3},
])
def test_session_end_missing_fields_is_noop(payload, tmp_path, monkeypatch, fake_popen):
    code, out, _err = run("session-end", payload, tmp_path, monkeypatch)
    assert code == 0 and out == ""
    assert fake_popen == [] and _job_files(tmp_path) == []

# C6: recursion guard — AUSTIN_POWER_EXTRACTOR_CHILD=1 makes every event a silent no-op.
@pytest.mark.parametrize("event", ["post-compact", "session-start", "session-end"])
def test_extractor_child_short_circuits_every_event(event, tmp_path, monkeypatch, fake_popen):
    code, out, err = run(event, {"session_id": "s", "cwd": str(tmp_path), "compact_summary": "x" * 250,
                                  "transcript_path": str(tmp_path / "t.jsonl")},
                          tmp_path, monkeypatch, extra_env={"AUSTIN_POWER_EXTRACTOR_CHILD": "1"})
    assert code == 0 and out == "" and err == ""
    assert fake_popen == [] and _job_files(tmp_path) == []
    assert not (tmp_path / "h").exists()

def test_resolve_project(tmp_path):
    assert hook.resolve_project(str(tmp_path), {"AUSTIN_POWER_PROJECT": "x"}) == "x"
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    (tmp_path / "repo" / "sub").mkdir()
    assert hook.resolve_project(str(tmp_path / "repo" / "sub"), {}) == "repo"
    assert hook.resolve_project(str(tmp_path), {}) == tmp_path.name
    assert hook.resolve_project("", {}) == ""
