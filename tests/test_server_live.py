"""Live smoke test: `serve()` in a real subprocess, over a real socket.

Task 5's CLI (`austin-power serve`) doesn't exist yet, so this drives
`austin_power.server.serve()` directly via `python -c` per the plan's fallback.
"""

import json
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.kiwi


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001 - retry until the server is up
            last_exc = e
            time.sleep(0.2)
    raise AssertionError(f"server never became healthy on port {port}: {last_exc}")


def _spawn(home, port):
    code = (
        "from austin_power.server import serve; "
        "from austin_power.config import load_config; "
        f"raise SystemExit(serve(load_config(port={port})))"
    )
    env = {"AUSTIN_POWER_HOME": str(home), "PATH": __import__("os").environ.get("PATH", "")}
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_serve_lifecycle_and_second_instance_refused(tmp_path):
    home = tmp_path / "h"
    port = _free_port()
    proc = _spawn(home, port)
    try:
        info = _wait_health(port)
        assert info["name"] == "austin-power" and info["status"] == "ok"

        # A second server for the same home must refuse to start (ServerLock).
        proc2 = _spawn(home, _free_port())
        rc2 = proc2.wait(timeout=15)
        assert rc2 == 1

        token = (home / "token").read_text().strip()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "save",
                    "arguments": {"title": "라이브 테스트", "body": "smoke test body"},
                },
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read())
        result = payload["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["action"] == "created"
    finally:
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
        _, err = proc.communicate()
        # uvicorn 0.53 runs a full graceful shutdown on SIGTERM (startup ->
        # "Waiting for application shutdown" -> "Finished server process"),
        # then deliberately re-raises the captured signal afterwards
        # (Server.capture_signals, by design) so the process itself dies of
        # SIGTERM rather than exiting 0 -- verified directly against the
        # installed uvicorn source and by running this subprocess standalone.
        assert rc in (0, -signal.SIGTERM)
        assert "Finished server process" in err
