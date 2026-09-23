from pathlib import Path

import pytest

from austin_power.config import ConfigError, load_config


def test_defaults_under_xdg(tmp_path):
    cfg = load_config(env={"XDG_CONFIG_HOME": str(tmp_path)})
    assert cfg.home == (tmp_path / "austin-power").resolve()
    assert cfg.db_path == cfg.home / "memory.db"
    assert cfg.token_path == cfg.home / "token"
    assert cfg.lock_path == cfg.home / "server.lock"
    assert (cfg.host, cfg.port, cfg.inject_chars) == ("127.0.0.1", 7760, 4000)
    assert cfg.mcp_url == "http://127.0.0.1:7760/mcp"

def test_home_env_wins(tmp_path):
    cfg = load_config(env={"AUSTIN_POWER_HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": "/nope"})
    assert cfg.home == (tmp_path / "h").resolve()

def test_relative_db_is_relative_to_home_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(env={"AUSTIN_POWER_HOME": str(tmp_path / "h"), "AUSTIN_POWER_DB": "sub/m.db"})
    assert cfg.db_path == (tmp_path / "h" / "sub" / "m.db").resolve()

def test_relative_home_is_relative_to_user_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = load_config(env={"AUSTIN_POWER_HOME": "rel"})
    assert cfg.home == (tmp_path / "rel").resolve()

@pytest.mark.parametrize("port", ["0", "65536", "abc", "-1"])
def test_bad_port(port, tmp_path):
    with pytest.raises(ConfigError):
        load_config(env={"AUSTIN_POWER_HOME": str(tmp_path), "AUSTIN_POWER_PORT": port})

def test_cli_overrides_env(tmp_path):
    cfg = load_config(host="::1", port=8000, env={"AUSTIN_POWER_HOME": str(tmp_path), "AUSTIN_POWER_PORT": "9000"})
    assert cfg.port == 8000 and cfg.mcp_url == "http://[::1]:8000/mcp"

def test_require_loopback():
    from austin_power.config import require_loopback
    for h in ("127.0.0.1", "::1", "localhost"):
        require_loopback(h)
    with pytest.raises(ConfigError):
        require_loopback("0.0.0.0")

def test_ensure_home_mode(tmp_path):
    from austin_power.config import ensure_home
    cfg = load_config(env={"AUSTIN_POWER_HOME": str(tmp_path / "h")})
    ensure_home(cfg)
    assert (cfg.home.stat().st_mode & 0o777) == 0o700
