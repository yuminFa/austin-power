#!/usr/bin/env python3
"""Measure austin-power server RSS/latency against the spec §2.5.1 budget.

Original spec U8: fail if the increase after 1,000 alternating save/search
calls is more than 20% over the startup RSS (evidence there's no unbounded
per-session or per-call accumulation) — kept as `run_measurement`/growth_pct,
informational only now that §2.5.1 redefines the acceptance thresholds.

Spec §2.5.1 re-measurement (after moving Kiwi into a worker child process),
checked against these targets and gating the exit code:
  - idle server RSS <= 120MB (before any call spawns the worker)
  - active total RSS (server + kiwi_worker child) <= 650MB
  - first-call latency after an idle worker unload <= 3s
  - time to rebuild 1,000 notes (worker backend) <= 15s

Run directly (`uv run python scripts/measure_rss.py`) against real subprocess
servers over loopback HTTP — no external services.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import apsw

from austin_power import hook
from austin_power.config import load_config


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return
        except Exception as e:  # noqa: BLE001 - retry until the server is up
            last_exc = e
            time.sleep(0.2)
    raise RuntimeError(f"server never became healthy on port {port}: {last_exc}")


def read_rss_mb(pid: int) -> float:
    """Resident set size for `pid` in MB, via `ps` (macOS/Linux; both support -o rss=)."""
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
    )
    kb = int(out.stdout.strip())
    return kb / 1024


def read_rss_mb_or_none(pid: int) -> float | None:
    """Like read_rss_mb, but None (instead of raising) if `pid` has already exited."""
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return int(out.stdout.strip()) / 1024


def read_total_rss_mb(pids: list[int]) -> float:
    """Sum of RSS across several pids; a pid that has already exited contributes 0."""
    return sum(v for pid in pids if (v := read_rss_mb_or_none(pid)) is not None)


def find_child_pids(parent_pid: int) -> list[int]:
    """Direct child pids of `parent_pid` (macOS/Linux; both support `ps -eo pid=,ppid=`)."""
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=True
    )
    kids = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if ppid == parent_pid:
            kids.append(pid)
    return kids


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def growth_pct(before_mb: float, after_mb: float) -> float:
    if before_mb <= 0:
        raise ValueError(f"before_mb must be positive, got {before_mb!r}")
    return (after_mb - before_mb) / before_mb * 100


def spawn_server(home: Path, port: int, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    code = (
        "from austin_power.server import serve; "
        "from austin_power.config import load_config; "
        f"raise SystemExit(serve(load_config(port={port})))"
    )
    env = {"AUSTIN_POWER_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if extra_env:
        env.update(extra_env)
    # Discard stdio: the server logs one line per request, and at 1,000
    # requests an unread PIPE fills its OS buffer and the server blocks on
    # the next write — which looks like the *client* timing out mid-run.
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_server(proc: subprocess.Popen, home: Path | None = None, *, remove_home: bool = False) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    if remove_home and home is not None:
        shutil.rmtree(home, ignore_errors=True)


def run_measurement(iterations: int = 1000, home: Path | None = None) -> dict:
    own_home = home is None
    home = home or Path(tempfile.mkdtemp(prefix="austin-power-rss-"))
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = spawn_server(home, port)
    try:
        wait_health(port)
        startup_rss_mb = read_rss_mb(proc.pid)

        cfg = load_config(port=port, env={"AUSTIN_POWER_HOME": str(home)})
        token = (home / "token").read_text().strip()

        after_first_call_rss_mb = None
        for i in range(iterations):
            if i % 2 == 0:
                hook.call_tool(
                    cfg,
                    token,
                    "save",
                    {"title": f"rss-measure {i}", "body": f"측정 본문 {i}", "project": "rss-measure"},
                )
            else:
                hook.call_tool(cfg, token, "search", {"query": "측정"})
            if i == 0:
                # The Kiwi model lazy-loads on the first call that sees
                # non-ASCII text (tokenizer.get_kiwi, lru_cache), not at
                # startup — so most of any startup->after-N growth is this
                # one-time load, not per-call accumulation. Recorded
                # separately so that's visible instead of guessed at.
                after_first_call_rss_mb = read_rss_mb(proc.pid)

        after_1000_rss_mb = read_rss_mb(proc.pid)
    finally:
        stop_server(proc, home, remove_home=own_home)

    return {
        "startup_rss_mb": round(startup_rss_mb, 1),
        "after_first_call_rss_mb": round(after_first_call_rss_mb, 1),
        "after_1000_rss_mb": round(after_1000_rss_mb, 1),
        "growth_pct": round(growth_pct(startup_rss_mb, after_1000_rss_mb), 1),
    }


def run_worker_memory_measurement(iterations: int = 1000, home: Path | None = None) -> dict:
    """Spec §2.5.1: idle server-process RSS (the worker may already be running —
    `db.open_db()` calls `tokenizer.ensure_ready()` unconditionally at server
    startup, so the worker spawns before /health responds, not lazily on a
    client's first call; "idle" here means the *server* process alone, per
    spec's own split between the idle-server and active-total budgets) and the
    max server+kiwi_worker total RSS observed while making `iterations` calls."""
    own_home = home is None
    home = home or Path(tempfile.mkdtemp(prefix="austin-power-rss-worker-"))
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = spawn_server(home, port)
    try:
        wait_health(port)
        idle_rss_mb = read_rss_mb(proc.pid)

        cfg = load_config(port=port, env={"AUSTIN_POWER_HOME": str(home)})
        token = (home / "token").read_text().strip()

        max_total_rss_mb = idle_rss_mb
        sample_at = {0, max(iterations // 4, 1), max(iterations // 2, 1), max((3 * iterations) // 4, 1), iterations - 1}
        for i in range(iterations):
            if i % 2 == 0:
                hook.call_tool(
                    cfg,
                    token,
                    "save",
                    {"title": f"worker-rss {i}", "body": f"메모리 측정 {i}", "project": "worker-rss-measure"},
                )
            else:
                hook.call_tool(cfg, token, "search", {"query": "측정"})
            if i in sample_at:
                total = read_total_rss_mb([proc.pid, *find_child_pids(proc.pid)])
                max_total_rss_mb = max(max_total_rss_mb, total)
    finally:
        stop_server(proc, home, remove_home=own_home)

    return {
        "idle_rss_mb": round(idle_rss_mb, 1),
        "active_total_rss_mb": round(max_total_rss_mb, 1),
    }


def run_idle_reload_measurement(
    home: Path | None = None, idle_seconds: int = 2, settle_s: float = 4.0
) -> dict:
    """Spec §2.5.1: with AUSTIN_POWER_KIWI_IDLE=<idle_seconds>, make one call to
    obtain the current worker's pid (it's already running by then — spawned at
    server startup by `db.open_db()`'s unconditional `tokenizer.ensure_ready()`,
    not lazily on this call), wait `settle_s` for the idle-unload timer to kill
    it, confirm it's gone, then time the next call (which must respawn and
    re-handshake the worker before it can tokenize)."""
    own_home = home is None
    home = home or Path(tempfile.mkdtemp(prefix="austin-power-rss-idle-reload-"))
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = spawn_server(home, port, extra_env={"AUSTIN_POWER_KIWI_IDLE": str(idle_seconds)})
    try:
        wait_health(port)
        cfg = load_config(port=port, env={"AUSTIN_POWER_HOME": str(home)})
        token = (home / "token").read_text().strip()

        hook.call_tool(
            cfg, token, "save",
            {"title": "idle-reload warm", "body": "예열 저장", "project": "idle-reload-measure"},
            timeout=15.0,
        )
        worker_pids = find_child_pids(proc.pid)
        if not worker_pids:
            raise RuntimeError("kiwi worker did not spawn after the first Korean-text call")

        time.sleep(settle_s)
        worker_gone = not any(pid_alive(pid) for pid in worker_pids)
        if not worker_gone:
            raise RuntimeError(
                f"kiwi worker still alive {settle_s}s after AUSTIN_POWER_KIWI_IDLE={idle_seconds}s"
                " — first_call_after_idle_s below would not measure a real reload"
            )

        t0 = time.monotonic()
        hook.call_tool(
            cfg, token, "save",
            {"title": "idle-reload after", "body": "재기동 후 저장", "project": "idle-reload-measure"},
            timeout=15.0,
        )
        first_call_after_idle_s = time.monotonic() - t0
    finally:
        stop_server(proc, home, remove_home=own_home)

    return {
        "worker_gone_after_idle": worker_gone,
        "first_call_after_idle_s": round(first_call_after_idle_s, 2),
    }


def run_rebuild_measurement(notes: int = 1000, home: Path | None = None) -> dict:
    """Spec §2.5.1: create `notes` notes, force a tokenizer_sig mismatch (as if
    austin-power's tokenizer rules changed), restart the server, and measure
    startup-to-health time. `db.open_db()` runs synchronously before the server
    binds its socket and spawns+handshakes the kiwi worker before rebuilding,
    so this includes interpreter/import startup and the worker's model load in
    addition to the `worker`-backend FTS5 rebuild itself (all on the critical
    path to /health, so it's a fair "how long until the server is usable again"
    number even though it overstates the rebuild alone)."""
    own_home = home is None
    home = home or Path(tempfile.mkdtemp(prefix="austin-power-rss-rebuild-"))
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = spawn_server(home, port)
    try:
        wait_health(port)
        cfg = load_config(port=port, env={"AUSTIN_POWER_HOME": str(home)})
        token = (home / "token").read_text().strip()
        for i in range(notes):
            hook.call_tool(
                cfg,
                token,
                "save",
                {"title": f"rebuild-note-{i}", "body": f"재색인 테스트 본문 {i}", "project": "rebuild-measure"},
            )
    finally:
        stop_server(proc)

    conn = apsw.Connection(str(home / "memory.db"))
    try:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='tokenizer_sig'", ("test-forced-rebuild",)
        )
    finally:
        conn.close()

    port2 = free_port()
    proc2 = spawn_server(home, port2)
    try:
        t0 = time.monotonic()
        wait_health(port2, timeout=60.0)
        rebuild_1000_s = time.monotonic() - t0
    finally:
        stop_server(proc2, home, remove_home=own_home)

    return {"rebuild_1000_s": round(rebuild_1000_s, 2)}


def check_acceptance(results: dict) -> dict[str, bool]:
    """Spec §2.5.1 "효과 목표" thresholds. All comparisons are <=, matching the
    spec's own wording, so a measurement exactly at the threshold passes."""
    return {
        "idle_rss_mb<=120": results["idle_rss_mb"] <= 120,
        "active_total_rss_mb<=650": results["active_total_rss_mb"] <= 650,
        "first_call_after_idle_s<=3": results["first_call_after_idle_s"] <= 3,
        "rebuild_1000_s<=15": results["rebuild_1000_s"] <= 15,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--iterations", type=int, default=1000)
    p.add_argument("--rebuild-notes", type=int, default=1000)
    args = p.parse_args(argv)

    growth = run_measurement(iterations=args.iterations)
    worker = run_worker_memory_measurement(iterations=args.iterations)
    idle_reload = run_idle_reload_measurement()
    rebuild = run_rebuild_measurement(notes=args.rebuild_notes)

    results = {**growth, **worker, **idle_reload, **rebuild}
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print(
        f"info: growth_pct over {args.iterations} calls = {growth['growth_pct']}%"
        " (original spec U8 threshold, informational — superseded by §2.5.1 below)"
    )

    acceptance = check_acceptance(results)
    labels = {
        "idle_rss_mb<=120": ("idle server RSS <= 120MB", results["idle_rss_mb"], "MB"),
        "active_total_rss_mb<=650": (
            "active total RSS (server+worker) <= 650MB", results["active_total_rss_mb"], "MB",
        ),
        "first_call_after_idle_s<=3": (
            "first call after idle unload <= 3s", results["first_call_after_idle_s"], "s",
        ),
        "rebuild_1000_s<=15": (
            f"rebuild {args.rebuild_notes} notes <= 15s", results["rebuild_1000_s"], "s",
        ),
    }
    for key, passed in acceptance.items():
        label, value, unit = labels[key]
        print(f"{'PASS' if passed else 'FAIL'}: {label} (measured {value}{unit})", file=sys.stderr)

    return 0 if all(acceptance.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
