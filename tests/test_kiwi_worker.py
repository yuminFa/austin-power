"""Spec §2.5.1: Kiwi runs in a worker process behind the server; tests below
cover each bullet of that section's "검증" list plus the worker module's own
import hygiene (no mcp/uvicorn) and wire contract.

`tokenizer._backend`/`WorkerBackend._proc` are reached into directly here
(white-box) since that's the only way to observe worker lifecycle from the
outside; it's fine for a test file dedicated to this one subsystem.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import apsw
import pytest

from austin_power import db, tokenizer

pytestmark = pytest.mark.kiwi

CORPUS = [
    "",
    "   ",
    "note_fts를 만들었다",
    "better-sqlite3를 썼다",
    "apsw가 좋다 v0.1",
    "漢字 문서를 검토했다",
    "워커가 인덱스를 다시 만들었습니다",
    "FTS5는 빠르다",
    "Hello World-2",
]


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    tokenizer.shutdown()
    tokenizer.set_backend("inproc")


# --- helpers ---------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_child_pid(server_pid: int, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["ps", "-o", "pid=,ppid=,command=", "-A"], capture_output=True, text=True, check=False
        ).stdout
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid_s, ppid_s, cmd = parts
            if ppid_s == str(server_pid) and "kiwi_worker" in cmd:
                return int(pid_s)
        time.sleep(0.2)
    raise AssertionError(f"no kiwi_worker child found for server pid {server_pid} within {timeout}s")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return
        except Exception as e:  # noqa: BLE001 - retry until the server is up
            last_exc = e
            time.sleep(0.2)
    raise AssertionError(f"server never became healthy on port {port}: {last_exc}")


def _spawn_serve(home, port, env_extra=None):
    code = (
        "from austin_power.server import serve; "
        "from austin_power.config import load_config; "
        f"raise SystemExit(serve(load_config(port={port})))"
    )
    env = {"AUSTIN_POWER_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    env.update(env_extra or {})
    return subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


# --- backend parity ---------------------------------------------------------


def test_backend_parity_across_varied_corpus():
    tokenizer.set_backend("inproc")
    expected = {(t, q): tokenizer.analyze(t, for_query=q) for t in CORPUS for q in (False, True)}

    tokenizer.set_backend("worker", idle_seconds=0)
    for (text, for_query), exp in expected.items():
        got = tokenizer.analyze(text, for_query=for_query)
        assert got == exp, f"worker backend mismatch for {text!r} (for_query={for_query})"


# --- idle unload + respawn ---------------------------------------------------


def test_idle_unload_then_respawn():
    tokenizer.set_backend("worker", idle_seconds=1)
    tokenizer.ensure_ready()
    proc1 = tokenizer._backend._proc
    assert proc1 is not None
    assert _pid_alive(proc1.pid)

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _pid_alive(proc1.pid):
        time.sleep(0.2)
    assert not _pid_alive(proc1.pid), "worker did not unload after idle_seconds + kill grace"

    result = tokenizer.analyze("워커가 다시 떴다", for_query=False)
    assert result
    proc2 = tokenizer._backend._proc
    assert proc2 is not None and proc2.pid != proc1.pid


# --- kill mid-session -> retry recovers -------------------------------------


def test_kill_worker_mid_session_recovers_via_retry():
    tokenizer.set_backend("worker", idle_seconds=0)
    text = "워커를 죽여도 다음 호출은 성공한다"
    r1 = tokenizer.analyze(text, for_query=False)
    proc = tokenizer._backend._proc
    assert proc is not None

    os.kill(proc.pid, signal.SIGKILL)

    r2 = tokenizer.analyze(text, for_query=False)
    assert r2 == r1
    assert tokenizer._backend._proc is not None
    assert tokenizer._backend._proc.pid != proc.pid


# --- signature mismatch rejected --------------------------------------------


def test_signature_mismatch_is_rejected(monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER__TEST_FAKE_SIG", "totally-different-signature")
    tokenizer.set_backend("worker", idle_seconds=0)
    with pytest.raises(tokenizer.TokenizerMismatchError):
        tokenizer.ensure_ready()
    assert tokenizer._backend._proc is None


def test_db_reexports_tokenizer_mismatch_error():
    assert db.TokenizerMismatchError is tokenizer.TokenizerMismatchError


# --- SIGTERM of `austin-power serve` leaves no kiwi_worker child ------------


def test_sigterm_of_serve_leaves_no_kiwi_worker_child(tmp_path):
    home = tmp_path / "h"
    port = _free_port()
    proc = _spawn_serve(home, port)
    try:
        _wait_health(port)
        worker_pid = _worker_child_pid(proc.pid)
        assert _pid_alive(worker_pid)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(worker_pid):
            time.sleep(0.1)
        assert not _pid_alive(worker_pid), "kiwi_worker child survived SIGTERM of the server"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- parent SIGKILL -> worker exits via stdin EOF within 5s -----------------


def test_sigkill_of_parent_worker_exits_via_eof(tmp_path):
    home = tmp_path / "h"
    port = _free_port()
    proc = _spawn_serve(home, port)
    try:
        _wait_health(port)
        worker_pid = _worker_child_pid(proc.pid)

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(worker_pid):
            time.sleep(0.1)
        assert not _pid_alive(worker_pid), "kiwi_worker child did not exit within 5s of parent SIGKILL"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- worker spawn happens outside the write lock ----------------------------


def test_ensure_ready_runs_before_begin_immediate_not_inside_it(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    conn_a = db.open_db(path)

    entered = threading.Event()
    release = threading.Event()

    def fake_ensure_ready():
        entered.set()
        assert release.wait(timeout=5), "test itself deadlocked"

    monkeypatch.setattr(tokenizer, "ensure_ready", fake_ensure_ready)

    def writer():
        with db.write_txn(conn_a):
            conn_a.execute(
                "insert into note(project,kind,title,body,created_at,updated_at) "
                "values('','fact','t','b',1,1)"
            )

    th = threading.Thread(target=writer)
    th.start()
    try:
        assert entered.wait(timeout=5), "writer thread never reached ensure_ready()"
        # If ensure_ready() ran *inside* BEGIN IMMEDIATE, conn_a would already
        # hold SQLite's write lock here and this would raise BusyError.
        conn_b = apsw.Connection(str(path))
        conn_b.set_busy_timeout(200)
        conn_b.execute("BEGIN IMMEDIATE")
        conn_b.execute("COMMIT")
        conn_b.close()
    finally:
        release.set()
        th.join(timeout=5)
    conn_a.close()


# --- worker module hygiene ---------------------------------------------------


def test_worker_module_does_not_import_mcp_or_uvicorn():
    code = (
        "import sys; import austin_power.kiwi_worker; "
        "print(','.join(sorted(m for m in ('mcp', 'uvicorn') if m in sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"kiwi_worker pulled in: {out.stdout.strip()}"


def test_worker_wire_protocol_directly():
    proc = subprocess.Popen(
        [sys.executable, "-m", "austin_power.kiwi_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        ready = proc.stdout.readline()
        assert ready, proc.stderr.read()
        import json

        msg = json.loads(ready)
        assert msg["ready"] is True
        assert msg["signature"] == tokenizer.signature()

        proc.stdin.write(json.dumps({"text": "note_fts를 만들었다"}) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert "tokens" in resp
        forms = [t[0] for t in resp["tokens"]]
        assert "만들" in forms

        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- default backend is inproc (import, hook fallback, tests) --------------


def test_default_backend_is_inproc():
    """Item (b): default backend is inproc, so the post-compact hook fallback
    (which never calls set_backend) never spawns a worker process."""
    assert isinstance(tokenizer._backend, tokenizer._InprocBackend)
