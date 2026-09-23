#!/usr/bin/env python3
"""Measure austin-power server RSS at startup and after 1,000 tool calls.

Spec U8: fail if the increase after 1,000 alternating save/search calls is
more than 20% over the startup RSS (evidence there's no unbounded per-session
or per-call accumulation). Run directly (`uv run python scripts/measure_rss.py`)
against a real subprocess server over loopback HTTP — no external services.
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


def growth_pct(before_mb: float, after_mb: float) -> float:
    if before_mb <= 0:
        raise ValueError(f"before_mb must be positive, got {before_mb!r}")
    return (after_mb - before_mb) / before_mb * 100


def spawn_server(home: Path, port: int) -> subprocess.Popen:
    code = (
        "from austin_power.server import serve; "
        "from austin_power.config import load_config; "
        f"raise SystemExit(serve(load_config(port={port})))"
    )
    env = {"AUSTIN_POWER_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    # Discard stdio: the server logs one line per request, and at 1,000
    # requests an unread PIPE fills its OS buffer and the server blocks on
    # the next write — which looks like the *client* timing out mid-run.
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if own_home:
            shutil.rmtree(home, ignore_errors=True)

    return {
        "startup_rss_mb": round(startup_rss_mb, 1),
        "after_first_call_rss_mb": round(after_first_call_rss_mb, 1),
        "after_1000_rss_mb": round(after_1000_rss_mb, 1),
        "growth_pct": round(growth_pct(startup_rss_mb, after_1000_rss_mb), 1),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--iterations", type=int, default=1000)
    args = p.parse_args(argv)

    result = run_measurement(iterations=args.iterations)
    print(json.dumps(result, indent=2))
    if result["growth_pct"] > 20:
        print(f"FAIL: growth_pct {result['growth_pct']} > 20 (spec U8)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
