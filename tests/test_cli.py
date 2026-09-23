import json
import subprocess
import sys

from austin_power.cli import main


def run(argv, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    code = main(argv)
    return code, capsys.readouterr()


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


def test_setup_rejects_non_loopback(tmp_path, capsys, monkeypatch):
    code, _ = run(["setup", "claude", "--host", "0.0.0.0"], tmp_path, capsys, monkeypatch)
    assert code == 2


def test_status_not_running(tmp_path, capsys, monkeypatch):
    code, out = run(["status", "--port", "1"], tmp_path, capsys, monkeypatch)
    assert code == 1 and "not running" in out.out
    assert not (tmp_path / "h").exists()  # status never creates home


def test_bad_port_exit_2(tmp_path, capsys, monkeypatch):
    code, _ = run(["status", "--port", "abc"], tmp_path, capsys, monkeypatch)
    assert code == 2


def test_python_m(tmp_path):
    r = subprocess.run([sys.executable, "-m", "austin_power", "--version"], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and "0.1.0" in r.stdout
