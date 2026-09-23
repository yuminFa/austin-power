from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "austin-power"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7760
DEFAULT_INJECT_CHARS = 4000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

class ConfigError(ValueError):
    pass

@dataclass(frozen=True)
class Config:
    home: Path
    db_path: Path
    token_path: Path
    lock_path: Path
    host: str
    port: int
    inject_chars: int
    log_level: str

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return self.base_url + "/mcp"

def _abs(value: str, base: Path) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else base / p).resolve()

def resolve_home(env: Mapping[str, str]) -> Path:
    user_home = Path.home()
    if v := env.get("AUSTIN_POWER_HOME"):
        return _abs(v, user_home)
    if v := env.get("XDG_CONFIG_HOME"):
        return _abs(v, user_home) / APP
    if sys.platform == "win32" and (v := env.get("APPDATA")):
        return _abs(v, user_home) / APP
    return (user_home / ".config" / APP).resolve()

def _int(value, name: str, lo: int, hi: int) -> int:
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {n}")
    return n

def load_config(*, host=None, port=None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    home = resolve_home(env)
    db = _abs(env["AUSTIN_POWER_DB"], home) if env.get("AUSTIN_POWER_DB") else home / "memory.db"
    return Config(
        home=home,
        db_path=db,
        token_path=home / "token",
        lock_path=home / "server.lock",
        host=(host or env.get("AUSTIN_POWER_HOST") or DEFAULT_HOST).strip(),
        port=_int(port if port is not None else env.get("AUSTIN_POWER_PORT", DEFAULT_PORT), "port", 1, 65535),
        inject_chars=_int(env.get("AUSTIN_POWER_INJECT_CHARS", DEFAULT_INJECT_CHARS), "AUSTIN_POWER_INJECT_CHARS", 1, 1_000_000),
        log_level=env.get("AUSTIN_POWER_LOG_LEVEL", "INFO").upper(),
    )

def ensure_home(cfg: Config) -> None:
    cfg.home.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(cfg.home, 0o700)

def require_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(f"host {host!r} is not loopback; v0.1 only serves locally")
