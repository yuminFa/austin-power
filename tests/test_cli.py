import contextlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from austin_power.cli import main


def run(argv, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    code = main(argv)
    return code, capsys.readouterr()


@contextlib.contextmanager
def health_stub(payload: dict):
    """A local HTTP server on 127.0.0.1 that answers GET /health with `payload`
    as JSON, standing in for the real austin-power server's health check."""
    body = json.dumps(payload).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence request logging in test output
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_version(tmp_path, capsys, monkeypatch):
    code, out = run(["--version"], tmp_path, capsys, monkeypatch)
    assert code == 0 and "0.1.0" in out.out


def test_token_prints_and_is_stable(tmp_path, capsys, monkeypatch):
    c1, o1 = run(["token"], tmp_path, capsys, monkeypatch)
    c2, o2 = run(["token"], tmp_path, capsys, monkeypatch)
    assert c1 == c2 == 0 and o1.out.strip() == o2.out.strip() and len(o1.out.strip()) >= 40


def test_setup_claude(tmp_path, capsys, monkeypatch):
    code, out = run(["setup", "claude", "--port", "7799"], tmp_path, capsys, monkeypatch)
    assert code == 0
    assert (
        'claude mcp add --transport http --scope user austin-power http://127.0.0.1:7799/mcp --header "Authorization: Bearer '
        in out.out
    )
    assert "token" in out.err.lower()


def test_setup_codex(tmp_path, capsys, monkeypatch):
    _code, out = run(["setup", "codex"], tmp_path, capsys, monkeypatch)
    assert "codex mcp add austin-power --url http://127.0.0.1:7760/mcp --bearer-token-env-var AUSTIN_POWER_TOKEN" in out.out
    assert 'export AUSTIN_POWER_TOKEN="$(austin-power token)"' in out.out


def test_setup_hooks_is_valid_json(tmp_path, capsys, monkeypatch):
    _code, out = run(["setup", "hooks"], tmp_path, capsys, monkeypatch)
    hooks = json.loads(out.out)["hooks"]
    assert hooks["PostCompact"][0]["hooks"][0]["command"] == "austin-power hook post-compact"
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "austin-power hook session-start"
    assert hooks["SessionEnd"][0]["hooks"][0]["command"] == "austin-power hook session-end"
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 10


def test_hook_accepts_session_end_event(tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert main(["hook", "session-end"]) == 0


# C24 (spec §2.12): pre-compact is a hook event (Codex CLI), but never a
# Claude Code registration — it must not show up in `setup hooks` output.
def test_hook_accepts_pre_compact_event(tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert main(["hook", "pre-compact"]) == 0


def test_setup_hooks_omits_pre_compact(tmp_path, capsys, monkeypatch):
    _code, out = run(["setup", "hooks"], tmp_path, capsys, monkeypatch)
    assert "PreCompact" not in out.out
    assert "pre-compact" not in out.out


def test_hook_rejects_unknown_event(tmp_path, capsys, monkeypatch):
    code, _out = run(["hook", "bogus"], tmp_path, capsys, monkeypatch)
    assert code == 2


def test_setup_rejects_non_loopback(tmp_path, capsys, monkeypatch):
    code, _ = run(["setup", "claude", "--host", "0.0.0.0"], tmp_path, capsys, monkeypatch)
    assert code == 2


def test_status_not_running(tmp_path, capsys, monkeypatch):
    code, out = run(["status", "--port", "1"], tmp_path, capsys, monkeypatch)
    assert code == 1 and "not running" in out.out
    assert not (tmp_path / "h").exists()  # status never creates home


def test_status_ignores_env_proxy(tmp_path, capsys, monkeypatch):
    with health_stub({"status": "ok", "name": "austin-power", "version": "0.1.0"}) as port:
        # An HTTP proxy pointed at a closed port: if status honored it, the
        # request would fail (connection refused) instead of reaching the stub.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        code, out = run(["status", "--port", str(port)], tmp_path, capsys, monkeypatch)
    assert code == 0 and "running:" in out.out


def test_status_rejects_non_austin_power_payload(tmp_path, capsys, monkeypatch):
    with health_stub({"status": "ok", "name": "other"}) as port:
        code, out = run(["status", "--port", str(port)], tmp_path, capsys, monkeypatch)
    assert code == 1
    assert "not running:" in out.out and "port answered but it is not austin-power" in out.out


def test_bad_port_exit_2(tmp_path, capsys, monkeypatch):
    code, _ = run(["status", "--port", "abc"], tmp_path, capsys, monkeypatch)
    assert code == 2


def test_token_empty_file_errors_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir(parents=True)
    (tmp_path / "h" / "token").write_text("  \n")
    code = main(["token"])
    out = capsys.readouterr()
    assert code == 1
    assert out.err.startswith("error: ")
    assert "Traceback" not in out.err


def test_python_m(tmp_path):
    r = subprocess.run([sys.executable, "-m", "austin_power", "--version"], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and "0.1.0" in r.stdout
