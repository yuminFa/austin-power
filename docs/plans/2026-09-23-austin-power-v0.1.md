# austin-power v0.1 Implementation Plan

> **For agentic workers:** executed via the dev-pipeline-exec workflow (one task per agent, TDD red→green observed). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build a lightweight, Korean-aware MCP memory server: one resident HTTP MCP server over a single SQLite file with a Kiwi FTS5 tokenizer, plus CLI, JSONL import, Claude Code hooks, and an agentmemory converter.

**Architecture:** `austin_power` package with small single-purpose modules. `tokenizer` (Kiwi → FTS5) and `db` (connection/migration/lock) underpin `store` (pure SQL functions). `server` (mcp 2.x MCPServer + bearer middleware + uvicorn) and `importer`/`hook` (stdlib-only hot path) all call `store`. `cli` wires everything.

**Tech Stack:** Python ≥3.11, uv, apsw 3.53.4.0 (bundled SQLite 3.53.4), kiwipiepy ≥0.23, mcp ≥2.2,<3, uvicorn, pytest, ruff, hatchling.

**Spec:** `docs/specs/austin-power-v0.1-feature-spec.md` — the spec is the source of truth for every rule referenced below as "spec §x".

## Global Constraints

- Package/dist name `austin-power`, import name `austin_power`, console script `austin-power`, version `0.1.0`.
- `requires-python = ">=3.11"`; deps `mcp>=2.2,<3`, `apsw>=3.53.4.0`, `kiwipiepy>=0.23`, `uvicorn>=0.31`. Dev: `pytest>=8`, `ruff>=0.6`, `httpx>=0.27`.
- Defaults: host `127.0.0.1`, port `7760`, inject chars `4000`, DB `<home>/memory.db`, token `<home>/token`, lock `<home>/server.lock`.
- Env vars: `AUSTIN_POWER_HOME`, `AUSTIN_POWER_DB`, `AUSTIN_POWER_HOST`, `AUSTIN_POWER_PORT`, `AUSTIN_POWER_INJECT_CHARS`, `AUSTIN_POWER_LOG_LEVEL`, `AUSTIN_POWER_PROJECT`, `AUSTIN_POWER_TOKEN` (Codex only, not read by us).
- Limits: title 1–200, body 1–32,000, project 0–100, session_id 0–200, query 1–500, limit 1–50, kind `^[a-z0-9][a-z0-9_-]{0,31}$`.
- No personal paths, accounts, or company names anywhere in code, docs, tests, or examples.
- All tests must run offline except those marked `@pytest.mark.kiwi` which load the Kiwi model (still offline once installed). Always run tests with `uv run pytest`.
- Hook code path (`austin_power.hook` top level) must not import `kiwipiepy`, `apsw`, `mcp`, `uvicorn`, `anyio` at module import time.

## Review Focus

1. Mixed Korean + identifier text (`note_fts를`, `FTS5는`, `apsw가`) — search must behave as a Korean developer expects (particles ignored, identifiers exact). → Task 2 tests.
2. Concurrent writers across processes (server running while `import` or hook fallback writes) — no BusyError leak, no index divergence. → Task 3 (`test_signature_mismatch_refuses_write`, `test_second_lock_fails`) and Task 5 (`test_import_while_locked_refuses_rebuild`).
3. LLM-generated queries containing FTS5 syntax (`"`, `*`, `NEAR`, `-`, `:`) — never a syntax error. → Task 3 `test_search_fts_syntax_is_inert`.
4. Hook invoked in odd states (server down, token missing, malformed stdin, empty summary) — always exit 0, never garbage on stdout. → Task 6.
5. Re-running an old import must not clobber newer memories. → Task 5 `test_import_older_is_skipped`.

---

## File Structure

```
pyproject.toml, LICENSE, README.md, README.en.md, .github/workflows/ci.yml
src/austin_power/
  __init__.py      # __version__ = "0.1.0"
  __main__.py      # python -m austin_power → cli.main()
  config.py        # Config, load_config, resolve_home, ensure_home, require_loopback
  auth.py          # ensure_token, read_token, rotate_token, check_bearer
  tokenizer.py     # analyze(), query_tokens(), signature(), register()
  db.py            # open_db, open_db_readonly, ServerLock, write_txn, errors
  store.py         # validation + save/import_row/search/get/recent/forget
  server.py        # build_app, serve
  importer.py      # run_import
  hook.py          # post_compact, session_start (stdlib-only hot path)
  cli.py           # argparse entry
contrib/agentmemory_to_jsonl.py
examples/launchd/io.github.yuminfa.austin-power.plist, examples/systemd/austin-power.service
docs/design.md
tests/…
```

---

### Task 1: Project scaffold, config, auth

**Files:**
- Create: `pyproject.toml`, `LICENSE` (MIT, copyright "austin-power contributors"), `src/austin_power/__init__.py`, `src/austin_power/__main__.py`, `src/austin_power/config.py`, `src/austin_power/auth.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_auth.py`

**Interfaces:**
- Produces:
  - `config.Config` (frozen dataclass): `home: Path, db_path: Path, token_path: Path, lock_path: Path, host: str, port: int, inject_chars: int, log_level: str`; properties `base_url -> str` (`http://127.0.0.1:7760`, IPv6 bracketed), `mcp_url -> str` (`base_url + "/mcp"`).
  - `config.load_config(*, host: str|None=None, port: int|str|None=None, env: Mapping[str,str]|None=None) -> Config` (raises `ConfigError`)
  - `config.resolve_home(env) -> Path`, `config.ensure_home(cfg) -> None`, `config.require_loopback(host) -> None`, `config.ConfigError(ValueError)`, `config.LOOPBACK_HOSTS`
  - `auth.ensure_token(path: Path) -> str`, `auth.read_token(path: Path) -> str` (raises `TokenError`), `auth.rotate_token(path: Path) -> str`, `auth.check_bearer(header: str|None, token: str) -> bool`, `auth.TokenError(RuntimeError)`

- [ ] **Step 1: pyproject + package skeleton**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "austin-power"
version = "0.1.0"
description = "Lightweight, Korean-aware memory server for coding agents (MCP over local HTTP, SQLite FTS5 + Kiwi)"
readme = "README.md"
license = "MIT"
requires-python = ">=3.11"
dependencies = ["mcp>=2.2,<3", "apsw>=3.53.4.0", "kiwipiepy>=0.23", "uvicorn>=0.31"]

[project.scripts]
austin-power = "austin_power.cli:main"

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6", "httpx>=0.27"]

[tool.hatch.build.targets.wheel]
packages = ["src/austin_power"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["kiwi: loads the Kiwi model (slower)"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

`__init__.py`: `__version__ = "0.1.0"`. `__main__.py`: `from austin_power.cli import main; raise SystemExit(main())` (cli arrives in Task 5; until then create `cli.py` with `def main(argv=None) -> int: return 0` so imports work).

Run `uv sync` (creates `.venv`, `uv.lock`).

- [ ] **Step 2: failing tests for config** (`tests/test_config.py`)

```python
from pathlib import Path
import pytest
from austin_power.config import load_config, ConfigError

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
```

Run `uv run pytest tests/test_config.py -v` → FAIL (ImportError).

- [ ] **Step 3: implement `config.py`**

```python
from __future__ import annotations
import os, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
```

Run tests → PASS.

- [ ] **Step 4: failing tests for auth** (`tests/test_auth.py`)

```python
import os, threading
import pytest
from austin_power.auth import ensure_token, read_token, rotate_token, check_bearer, TokenError

def test_ensure_creates_0600_and_is_stable(tmp_path):
    p = tmp_path / "token"
    t1 = ensure_token(p)
    assert len(t1) >= 40 and ensure_token(p) == t1
    assert (p.stat().st_mode & 0o777) == 0o600
    assert not [f for f in tmp_path.iterdir() if f.name != "token"]  # temp file removed

def test_concurrent_creation_agrees(tmp_path):
    p = tmp_path / "token"
    out = []
    ts = [threading.Thread(target=lambda: out.append(ensure_token(p))) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(out)) == 1 and read_token(p) == out[0]

def test_read_empty_raises(tmp_path):
    p = tmp_path / "token"; p.write_text("  \n")
    with pytest.raises(TokenError):
        read_token(p)

def test_read_missing_raises(tmp_path):
    with pytest.raises(TokenError):
        read_token(tmp_path / "token")

def test_rotate_changes(tmp_path):
    p = tmp_path / "token"
    old = ensure_token(p)
    new = rotate_token(p)
    assert new != old and read_token(p) == new and (p.stat().st_mode & 0o777) == 0o600

@pytest.mark.parametrize("header,ok", [
    ("Bearer abc", True), ("bearer abc", True), ("BEARER abc", True),
    ("Bearer abcd", False), ("Basic abc", False), ("abc", False), ("", False), (None, False),
])
def test_check_bearer(header, ok):
    assert check_bearer(header, "abc") is ok
```

Run → FAIL.

- [ ] **Step 5: implement `auth.py`**

```python
from __future__ import annotations
import hmac, os, secrets
from pathlib import Path

class TokenError(RuntimeError):
    pass

def _write_tmp(path: Path, token: str) -> Path:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (token + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp

def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise TokenError(f"token file not found: {path}") from None
    if not token:
        raise TokenError(f"token file is empty: {path} — run `austin-power token --rotate`")
    return token

def ensure_token(path: Path) -> str:
    if path.exists():
        return read_token(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_tmp(path, secrets.token_urlsafe(32))
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        except OSError:
            if not path.exists():
                os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return read_token(path)

def rotate_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    os.replace(_write_tmp(path, token), path)
    return token

def check_bearer(header: str | None, token: str) -> bool:
    if not header:
        return False
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value.strip().encode(), token.encode())
```

Run `uv run pytest tests/test_config.py tests/test_auth.py -v` → PASS.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: project scaffold, config and token auth"`

---

### Task 2: Kiwi FTS5 tokenizer

**Files:**
- Create: `src/austin_power/tokenizer.py`, `tests/test_tokenizer.py`

**Interfaces:**
- Produces:
  - `tokenizer.RULES_VERSION = 1`
  - `tokenizer.analyze(text: str, *, for_query: bool) -> list[tuple[int, int, tuple[str, ...]]]` — char offsets `(start, end, (primary, *colocated))`, sorted by start.
  - `tokenizer.query_tokens(text: str) -> list[str]` — primaries of `analyze(text, for_query=True)`.
  - `tokenizer.signature() -> str` — `kiwi/1/kiwipiepy-<v>/model-<v>`.
  - `tokenizer.register(conn: apsw.Connection) -> None` — registers name `"kiwi"`.
  - `tokenizer.get_kiwi()` — cached `kiwipiepy.Kiwi()`.

- [ ] **Step 1: failing tests** (`tests/test_tokenizer.py`, module-level `pytestmark = pytest.mark.kiwi`)

```python
import apsw, pytest
from austin_power.tokenizer import analyze, query_tokens, register, signature

pytestmark = pytest.mark.kiwi

def prim(text, q=False):
    return [t[2][0] for t in analyze(text, for_query=q)]

def test_particles_removed_and_identifiers_kept():
    assert prim("note_fts를 만들었다") == ["note_fts", "만들"]
    assert prim("apsw가 좋다") == ["apsw", "좋"]
    assert prim("FTS5는 빠르다") == ["fts5", "빠르"]
    assert prim("better-sqlite3를 썼다") == ["better-sqlite3", "쓰"]

def test_colocated_only_when_indexing():
    doc = analyze("note_fts", for_query=False)
    assert doc == [(0, 8, ("note_fts", "note", "fts"))]
    assert analyze("note_fts", for_query=True) == [(0, 8, ("note_fts",))]

def test_only_particles_yield_nothing():
    assert query_tokens("을 를") == []
    assert query_tokens("   ") == []
    assert query_tokens("") == []

def test_ascii_only_skips_kiwi_and_offsets_are_chars():
    assert analyze("Hello World-2", for_query=True) == [(0, 5, ("hello",)), (6, 13, ("world-2",))]

def test_offsets_match_source_for_korean():
    text = "워커가 인덱스를 다시 만들었습니다"
    for s, e, toks in analyze(text, for_query=False):
        assert text[s:e]  # non-empty slice inside text
        assert 0 <= s < e <= len(text)

def test_signature_shape():
    sig = signature()
    assert sig.startswith("kiwi/1/kiwipiepy-") and "/model-" in sig

def _db():
    c = apsw.Connection(":memory:")
    register(c)
    c.execute("create virtual table f using fts5(b, tokenize='kiwi')")
    return c

def test_fts_particle_variants_match():
    c = _db()
    c.execute("insert into f(rowid, b) values (1, 'note_fts를 통째로 다시 만들었다')")
    c.execute("insert into f(rowid, b) values (2, 'note 하나만 있음')")
    hits = lambda q: [r[0] for r in c.execute("select rowid from f where f match ? order by rowid", (q,))]
    assert hits('"note_fts가"') == [1]
    assert hits('"note_fts"') == [1]          # does NOT match row 2
    assert hits('"fts"') == [1]
    assert hits('"만들다"') == [1]

def test_snippet_highlights_korean():
    c = _db()
    c.execute("insert into f(rowid, b) values (1, '워커가 뜰 때마다 인덱스를 다시 만들었습니다')")
    (s,) = c.execute("select snippet(f, 0, '[', ']', '…', 10) from f where f match '\"인덱스\"'").fetchone()
    assert "[인덱스]를" in s
```

Run `uv run pytest tests/test_tokenizer.py -v` → FAIL.

- [ ] **Step 2: implement `tokenizer.py`**

```python
from __future__ import annotations
import re
from functools import lru_cache
from importlib.metadata import version

import apsw
import apsw.fts5

RULES_VERSION = 1
ASCII_RUN = re.compile(r"[A-Za-z0-9_]+(?:[./-][A-Za-z0-9_]+)*")
_SEP = re.compile(r"[_./-]+")
_KEEP = ("NN", "VV", "VA", "XR", "SH", "SL", "SN")

@lru_cache(maxsize=1)
def get_kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()

def signature() -> str:
    return f"kiwi/{RULES_VERSION}/kiwipiepy-{version('kiwipiepy')}/model-{version('kiwipiepy_model')}"

def analyze(text: str, *, for_query: bool) -> list[tuple[int, int, tuple[str, ...]]]:
    out: list[tuple[int, int, tuple[str, ...]]] = []
    runs = [(m.start(), m.end()) for m in ASCII_RUN.finditer(text)]
    for s, e in runs:
        word = text[s:e].lower()
        toks = [word]
        if not for_query and _SEP.search(word):
            toks += [p for p in _SEP.split(word) if p and p != word]
        out.append((s, e, tuple(dict.fromkeys(toks))))
    if any(ord(ch) > 127 for ch in text):
        for t in get_kiwi().tokenize(text):
            s, e = t.start, t.start + t.len
            if e <= s or any(s < re_ and e > rs for rs, re_ in runs):
                continue
            if t.tag.startswith(_KEEP) and t.form.strip():
                out.append((s, e, (t.form.lower(),)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out

def query_tokens(text: str) -> list[str]:
    return [toks[0] for _, _, toks in analyze(text, for_query=True)]

@apsw.fts5.StringTokenizer
def _kiwi_tokenizer(con, args):
    def tokenize(text: str, flags: int, locale):
        for s, e, toks in analyze(text, for_query=bool(flags & apsw.FTS5_TOKENIZE_QUERY)):
            yield (s, e, *toks)
    return tokenize

def register(conn: apsw.Connection) -> None:
    conn.register_fts5_tokenizer("kiwi", _kiwi_tokenizer)
```

Note: `(s, e, *toks)` with more than one token emits colocated tokens (apsw `StringTokenizer` contract `yield start, end, *tokens`, verified in apsw source). Kiwi may return `form` that differs from the source slice (e.g. `썼` → `쓰`, `었`); offsets still come from `t.start/t.len`. If a Kiwi token's span is empty (`len == 0`) it is skipped.

Run tests → PASS. If `test_particles_removed_and_identifiers_kept` shows an extra token from Kiwi (e.g. `SN` for a digit inside an ASCII run), confirm it overlaps the run and is dropped by the overlap rule — do not relax the test.

- [ ] **Step 3: Commit** — `git commit -am "feat: Kiwi-based FTS5 tokenizer"` (add new files first).

---

### Task 3: Storage (db + store)

**Files:**
- Create: `src/austin_power/db.py`, `src/austin_power/store.py`, `tests/test_db.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `tokenizer.register`, `tokenizer.signature`, `tokenizer.query_tokens`.
- Produces (`db`):
  - `SCHEMA_VERSION = 1`; errors `SchemaTooNewError`, `TokenizerMismatchError` (both `RuntimeError`)
  - `open_db(path: Path, *, busy_timeout: int = 5000, rebuild_allowed: bool = True) -> apsw.Connection`
  - `open_db_readonly(path: Path) -> apsw.Connection | None`
  - `write_txn(conn) -> ContextManager[None]` — `BEGIN IMMEDIATE`, checks `meta.tokenizer_sig == tokenizer.signature()` (else rollback + `TokenizerMismatchError`), commits on success, rolls back on exception.
  - `ServerLock(path: Path)` with `.acquire(blocking: bool = False) -> bool`, `.release() -> None`, context manager support.
- Produces (`store`), all taking `conn` first:
  - `ValidationError(ValueError)` with `.field`
  - `normalize_kind(value: str) -> str`, validators used by `save`
  - `save(conn, *, title, body, project="", kind=None, session_id=None, now: int|None=None) -> tuple[int, str]` (`"created"|"updated"`)
  - `import_row(conn, row: ImportRow) -> str` (`"created"|"updated"|"skipped"`); `ImportRow` dataclass `title, body, project, kind: str|None, session_id: str|None, created_at: int|None, updated_at: int|None` (caller holds `write_txn`)
  - `plan_import_row(conn_or_none, row) -> str` — same decision without writing (for dry-run; `None` conn → `"created"`)
  - `search(conn, query, *, project=None, kind=None, limit=10) -> dict` → `{"match", "results": [{"id","project","kind","title","excerpt","updated_at"}]}`
  - `get(conn, note_id) -> dict | None`, `recent(conn, *, project=None, kind=None, limit=10) -> list[dict]`, `forget(conn, note_id) -> bool`
  - `iso(ts: int) -> str` (`2026-09-23T03:12:45Z`)

- [ ] **Step 1: failing tests for db** (`tests/test_db.py`, `pytestmark = pytest.mark.kiwi`)

```python
import apsw, pytest
from austin_power import db, tokenizer

pytestmark = pytest.mark.kiwi

def test_creates_schema_and_signature(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    assert c.execute("pragma user_version").fetchone()[0] == 1
    assert c.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert c.execute("select value from meta where key='tokenizer_sig'").fetchone()[0] == tokenizer.signature()

def test_reopen_is_idempotent(tmp_path):
    db.open_db(tmp_path / "m.db").close()
    c = db.open_db(tmp_path / "m.db")
    assert c.execute("select count(*) from sqlite_master where name='note'").fetchone()[0] == 1

def test_schema_too_new(tmp_path):
    c = db.open_db(tmp_path / "m.db"); c.execute("pragma user_version=2"); c.close()
    with pytest.raises(db.SchemaTooNewError):
        db.open_db(tmp_path / "m.db")

def test_signature_mismatch_rebuilds_when_allowed(tmp_path, caplog):
    c = db.open_db(tmp_path / "m.db")
    c.execute("insert into note(project,kind,title,body,created_at,updated_at) values('','fact','t','인덱스를',1,1)")
    c.execute("update meta set value='old' where key='tokenizer_sig'"); c.close()
    c = db.open_db(tmp_path / "m.db", rebuild_allowed=True)
    assert c.execute("select value from meta where key='tokenizer_sig'").fetchone()[0] == tokenizer.signature()
    assert c.execute("select rowid from note_fts where note_fts match '\"인덱스\"'").fetchall() == [(1,)]

def test_signature_mismatch_refuses_when_not_allowed(tmp_path):
    c = db.open_db(tmp_path / "m.db"); c.execute("update meta set value='old' where key='tokenizer_sig'"); c.close()
    with pytest.raises(db.TokenizerMismatchError):
        db.open_db(tmp_path / "m.db", rebuild_allowed=False)

def test_signature_mismatch_refuses_write(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    c.execute("update meta set value='other' where key='tokenizer_sig'")
    with pytest.raises(db.TokenizerMismatchError):
        with db.write_txn(c):
            c.execute("insert into note(project,kind,title,body,created_at,updated_at) values('','fact','t','b',1,1)")
    assert c.execute("select count(*) from note").fetchone()[0] == 0

def test_integrity_after_update_delete(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    with db.write_txn(c):
        c.execute("insert into note(project,kind,title,body,created_at,updated_at) values('','fact','t','하나',1,1)")
        c.execute("update note set body='둘' where id=1")
        c.execute("insert into note(project,kind,title,body,created_at,updated_at) values('','fact','u','셋',1,1)")
        c.execute("delete from note where id=2")
    c.execute("insert into note_fts(note_fts) values('integrity-check')")

def test_readonly_open(tmp_path):
    assert db.open_db_readonly(tmp_path / "none.db") is None
    db.open_db(tmp_path / "m.db").close()
    r = db.open_db_readonly(tmp_path / "m.db")
    assert r.execute("select count(*) from note").fetchone()[0] == 0
    with pytest.raises(apsw.ReadOnlyError):
        r.execute("insert into meta values('x','y')")

def test_second_lock_fails(tmp_path):
    a, b = db.ServerLock(tmp_path / "l"), db.ServerLock(tmp_path / "l")
    assert a.acquire() is True
    # flock is per open file description, so a second handle in the same process conflicts
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()
```

- [ ] **Step 2: implement `db.py`**

```python
from __future__ import annotations
import logging, os, sys
from contextlib import contextmanager
from pathlib import Path

import apsw
from austin_power import tokenizer

log = logging.getLogger("austin_power.db")
SCHEMA_VERSION = 1

class SchemaTooNewError(RuntimeError): ...
class TokenizerMismatchError(RuntimeError): ...

SCHEMA = """
CREATE TABLE note (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  session_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (project, title)
) STRICT;
CREATE INDEX note_project_updated ON note(project, updated_at DESC);
CREATE VIRTUAL TABLE note_fts USING fts5(title, body, content='note', content_rowid='id', tokenize='kiwi');
CREATE TRIGGER note_ai AFTER INSERT ON note BEGIN
  INSERT INTO note_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER note_ad AFTER DELETE ON note BEGIN
  INSERT INTO note_fts(note_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER note_au AFTER UPDATE ON note BEGIN
  INSERT INTO note_fts(note_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO note_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
"""

def _stored_sig(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key='tokenizer_sig'").fetchone()
    return row[0] if row else None

def open_db(path: Path, *, busy_timeout: int = 5000, rebuild_allowed: bool = True) -> apsw.Connection:
    tokenizer.get_kiwi()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(str(path))
    tokenizer.register(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.setbusytimeout(busy_timeout)
    sig = tokenizer.signature()
    conn.execute("BEGIN IMMEDIATE")
    try:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v > SCHEMA_VERSION:
            raise SchemaTooNewError(f"{path} was created by a newer austin-power (schema {v})")
        if v == 0:
            conn.execute(SCHEMA)
            conn.execute("INSERT INTO meta(key, value) VALUES ('tokenizer_sig', ?)", (sig,))
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif (old := _stored_sig(conn)) != sig:
            if not rebuild_allowed:
                raise TokenizerMismatchError(
                    f"tokenizer changed ({old} -> {sig}) while another austin-power server is running; restart the server")
            n = conn.execute("SELECT count(*) FROM note").fetchone()[0]
            conn.execute("INSERT INTO note_fts(note_fts) VALUES('rebuild')")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('tokenizer_sig', ?)", (sig,))
            log.info("reindexed %d notes: %s -> %s", n, old, sig)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    return conn

def open_db_readonly(path: Path) -> apsw.Connection | None:
    path = Path(path)
    if not path.exists():
        return None
    conn = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
    if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
        conn.close()
        raise SchemaTooNewError(f"{path} was created by a newer austin-power")
    return conn

@contextmanager
def write_txn(conn: apsw.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _stored_sig(conn) != tokenizer.signature():
            raise TokenizerMismatchError("index was rebuilt by a different austin-power version; restart this process")
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

class ServerLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self, blocking: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()  # closing the descriptor releases flock / msvcrt lock
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"lock busy: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
```

Run `uv run pytest tests/test_db.py -v` → PASS.

- [ ] **Step 3: failing tests for store** (`tests/test_store.py`, `pytestmark = pytest.mark.kiwi`, fixture `conn = db.open_db(tmp_path/"m.db")`)

```python
import pytest
from austin_power import db, store
from austin_power.store import ValidationError, ImportRow

pytestmark = pytest.mark.kiwi

@pytest.fixture
def conn(tmp_path):
    return db.open_db(tmp_path / "m.db")

def test_save_create_then_update_same_second(conn):
    assert store.save(conn, title="t", body="b", now=100) == (1, "created")
    assert store.save(conn, title="t", body="b2", now=100) == (1, "updated")
    assert conn.execute("select count(*), body from note").fetchone() == (1, "b2")

def test_save_keeps_kind_and_session_when_omitted(conn):
    store.save(conn, title="t", body="b", kind="Decision", session_id="s1", now=1)
    store.save(conn, title="t", body="b2", now=2)
    assert conn.execute("select kind, session_id, created_at, updated_at from note").fetchone() == ("decision", "s1", 1, 2)

def test_project_scopes_uniqueness(conn):
    store.save(conn, title="t", body="b", project="a")
    assert store.save(conn, title="t", body="b", project="b")[1] == "created"

@pytest.mark.parametrize("kw,field", [
    ({"title": "", "body": "b"}, "title"), ({"title": "x" * 201, "body": "b"}, "title"),
    ({"title": "t", "body": ""}, "body"), ({"title": "t", "body": "x" * 32001}, "body"),
    ({"title": "t", "body": "b", "kind": "bad kind"}, "kind"), ({"title": "t", "body": "b", "project": "p" * 101}, "project"),
    ({"title": "t", "body": "b", "session_id": "s" * 201}, "session_id"),
])
def test_save_validation(conn, kw, field):
    with pytest.raises(ValidationError) as e:
        store.save(conn, **kw)
    assert e.value.field == field

def test_limits_inclusive(conn):
    store.save(conn, title="x" * 200, body="y" * 32000)

def test_search_korean_particles(conn):
    store.save(conn, title="인덱스 재생성", body="워커가 뜰 때마다 note_fts 인덱스를 통째로 다시 만들었다")
    r = store.search(conn, "인덱스가 만들어졌나")
    assert r["match"] in ("all", "any") and r["results"][0]["title"] == "인덱스 재생성"
    assert "«" in r["results"][0]["excerpt"]

def test_search_and_then_or(conn):
    store.save(conn, title="a", body="사과 바나나")
    store.save(conn, title="b", body="사과 포도")
    assert [x["title"] for x in store.search(conn, "사과 바나나")["results"]] == ["a"]
    r = store.search(conn, "바나나 포도")
    assert r["match"] == "any" and {x["title"] for x in r["results"]} == {"a", "b"}

@pytest.mark.parametrize("q", ['"', '***', 'NEAR(a b)', '-x', 'title:foo', 'a AND OR', '^', '을 를'])
def test_search_fts_syntax_is_inert(conn, q):
    store.save(conn, title="t", body="본문")
    r = store.search(conn, q)
    assert r["match"] in ("none", "all", "any")

def test_search_filters_and_limit(conn):
    for i in range(3):
        store.save(conn, title=f"t{i}", body="공통 단어", project="p", kind="fact")
    store.save(conn, title="x", body="공통 단어", project="q", kind="bug")
    assert len(store.search(conn, "공통", project="p")["results"]) == 3
    assert len(store.search(conn, "공통", kind="bug")["results"]) == 1
    assert len(store.search(conn, "공통", limit=2)["results"]) == 2
    with pytest.raises(ValidationError):
        store.search(conn, "공통", limit=51)
    with pytest.raises(ValidationError):
        store.search(conn, "")

def test_title_weight(conn):
    store.save(conn, title="배포 절차", body="기타 내용")
    store.save(conn, title="기타", body="배포 이야기가 조금 나옴")
    assert store.search(conn, "배포")["results"][0]["title"] == "배포 절차"

def test_get_recent_forget(conn):
    nid, _ = store.save(conn, title="t", body="b" * 300, project="p", now=10)
    store.save(conn, title="u", body="c", project="p", now=20)
    g = store.get(conn, nid)
    assert g["body"] == "b" * 300 and g["created_at"] == "1970-01-01T00:00:10Z"
    rec = store.recent(conn, project="p")
    assert [x["title"] for x in rec] == ["u", "t"] and rec[1]["preview"].endswith("…")
    assert store.forget(conn, nid) is True and store.forget(conn, nid) is False
    assert store.get(conn, nid) is None

def test_import_rules(conn):
    r = lambda **k: ImportRow(**{"title": "t", "body": "b", "project": "", "kind": None, "session_id": None,
                                 "created_at": None, "updated_at": None, **k})
    with db.write_txn(conn):
        assert store.import_row(conn, r(created_at=10, updated_at=20)) == "created"
        assert store.import_row(conn, r(created_at=10, updated_at=20)) == "skipped"   # identical
        assert store.import_row(conn, r(updated_at=15, created_at=10)) == "skipped"   # older
        assert store.import_row(conn, r()) == "skipped"                              # no timestamps, exists
        assert store.import_row(conn, r(body="new", created_at=5, updated_at=30)) == "updated"
    assert conn.execute("select body, created_at, updated_at from note").fetchone() == ("new", 10, 30)
```

- [ ] **Step 4: implement `store.py`**

```python
from __future__ import annotations
import re, time
from dataclasses import dataclass
from datetime import datetime, timezone

import apsw.fts5query
from austin_power import db, tokenizer

KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TITLE_MAX, BODY_MAX, PROJECT_MAX, SESSION_MAX, QUERY_MAX, LIMIT_MAX = 200, 32000, 100, 200, 500, 50
PREVIEW = 200

class ValidationError(ValueError):
    def __init__(self, field: str, reason: str):
        super().__init__(f"invalid {field}: {reason}")
        self.field = field

def _text(field, value, lo, hi) -> str:
    if not isinstance(value, str):
        raise ValidationError(field, "must be a string")
    v = value.strip()
    if not lo <= len(v) <= hi:
        raise ValidationError(field, f"length must be {lo}..{hi} (got {len(v)})")
    return v

def normalize_kind(value) -> str:
    v = _text("kind", value, 1, 32).lower()
    if not KIND_RE.match(v):
        raise ValidationError("kind", "use lowercase letters, digits, '-' or '_' (max 32)")
    return v

def _limit(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIMIT_MAX:
        raise ValidationError("limit", f"must be an integer 1..{LIMIT_MAX}")
    return value

def _id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("id", "must be a positive integer")
    return value

def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _clean(title, body, project, kind, session_id):
    return (_text("title", title, 1, TITLE_MAX), _text("body", body, 1, BODY_MAX),
            _text("project", project or "", 0, PROJECT_MAX),
            None if kind is None else normalize_kind(kind),
            None if session_id is None else _text("session_id", session_id, 0, SESSION_MAX))

def _existing(conn, project, title):
    return conn.execute(
        "SELECT id, body, kind, session_id, created_at, updated_at FROM note WHERE project=? AND title=?",
        (project, title)).fetchone()

def save(conn, *, title, body, project="", kind=None, session_id=None, now=None) -> tuple[int, str]:
    title, body, project, kind, session_id = _clean(title, body, project, kind, session_id)
    now = int(time.time()) if now is None else int(now)
    with db.write_txn(conn):
        row = _existing(conn, project, title)
        if row is None:
            (nid,) = conn.execute(
                "INSERT INTO note(project, kind, title, body, session_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?) RETURNING id",
                (project, kind or "fact", title, body, session_id, now, now)).fetchone()
            return nid, "created"
        conn.execute(
            "UPDATE note SET body=?, kind=coalesce(?, kind), session_id=coalesce(?, session_id), updated_at=? WHERE id=?",
            (body, kind, session_id, now, row[0]))
        return row[0], "updated"

@dataclass(frozen=True)
class ImportRow:
    title: str
    body: str
    project: str
    kind: str | None
    session_id: str | None
    created_at: int | None
    updated_at: int | None

def _decide(row: ImportRow, existing) -> str:
    if existing is None:
        return "created"
    if row.updated_at is None:
        return "skipped"
    _, body, kind, sid, _, upd = existing
    if row.updated_at < upd:
        return "skipped"
    if row.updated_at == upd and row.body == body and (row.kind or kind) == kind and (row.session_id or sid) == sid:
        return "skipped"
    return "updated"

def plan_import_row(conn, row: ImportRow) -> str:
    return "created" if conn is None else _decide(row, _existing(conn, row.project, row.title))

def import_row(conn, row: ImportRow) -> str:
    """Caller must hold db.write_txn. Row fields are already validated/normalized."""
    existing = _existing(conn, row.project, row.title)
    action = _decide(row, existing)
    now = int(time.time())
    if action == "created":
        c = row.created_at if row.created_at is not None else now
        u = row.updated_at if row.updated_at is not None else c
        conn.execute("INSERT INTO note(project, kind, title, body, session_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (row.project, row.kind or "fact", row.title, row.body, row.session_id, c, u))
    elif action == "updated":
        conn.execute("UPDATE note SET body=?, kind=coalesce(?, kind), session_id=coalesce(?, session_id), updated_at=? WHERE id=?",
                     (row.body, row.kind, row.session_id, row.updated_at, existing[0]))
    return action

_SEARCH_SQL = """
SELECT n.id, n.project, n.kind, n.title, snippet(note_fts, -1, '«', '»', '…', 24), n.updated_at
FROM note_fts JOIN note n ON n.id = note_fts.rowid
WHERE note_fts MATCH ? AND (? IS NULL OR n.project = ?) AND (? IS NULL OR n.kind = ?)
ORDER BY bm25(note_fts, 3.0, 1.0), n.updated_at DESC
LIMIT ?
"""

def search(conn, query, *, project=None, kind=None, limit=10) -> dict:
    q = _text("query", query, 1, QUERY_MAX)
    project = None if project is None else _text("project", project, 0, PROJECT_MAX)
    kind = None if kind is None else normalize_kind(kind)
    limit = _limit(limit)
    parts = [p for p in q.split() if tokenizer.query_tokens(p)]
    if not parts:
        return {"match": "none", "results": []}
    quoted = [apsw.fts5query.quote(p) for p in parts]
    attempts = [("all", " ".join(quoted))] + ([("any", " OR ".join(quoted))] if len(quoted) > 1 else [])
    for label, expr in attempts:
        rows = conn.execute(_SEARCH_SQL, (expr, project, project, kind, kind, limit)).fetchall()
        if rows:
            return {"match": label, "results": [
                {"id": r[0], "project": r[1], "kind": r[2], "title": r[3], "excerpt": r[4], "updated_at": iso(r[5])}
                for r in rows]}
    return {"match": "none", "results": []}

def get(conn, note_id) -> dict | None:
    r = conn.execute("SELECT id, project, kind, title, body, session_id, created_at, updated_at FROM note WHERE id=?",
                     (_id(note_id),)).fetchone()
    if r is None:
        return None
    return {"id": r[0], "project": r[1], "kind": r[2], "title": r[3], "body": r[4], "session_id": r[5],
            "created_at": iso(r[6]), "updated_at": iso(r[7])}

def recent(conn, *, project=None, kind=None, limit=10) -> list[dict]:
    project = None if project is None else _text("project", project, 0, PROJECT_MAX)
    kind = None if kind is None else normalize_kind(kind)
    rows = conn.execute(
        "SELECT id, project, kind, title, body, updated_at FROM note "
        "WHERE (? IS NULL OR project=?) AND (? IS NULL OR kind=?) ORDER BY updated_at DESC, id DESC LIMIT ?",
        (project, project, kind, kind, _limit(limit))).fetchall()
    return [{"id": r[0], "project": r[1], "kind": r[2], "title": r[3],
             "preview": r[4] if len(r[4]) <= PREVIEW else r[4][:PREVIEW] + "…", "updated_at": iso(r[5])} for r in rows]

def forget(conn, note_id) -> bool:
    nid = _id(note_id)
    with db.write_txn(conn):
        return conn.execute("DELETE FROM note WHERE id=? RETURNING id", (nid,)).fetchone() is not None
```

Run `uv run pytest tests/test_db.py tests/test_store.py -v` → PASS. If `apsw.fts5query.quote` output for a term containing `"` differs, keep using it — the test only asserts no exception.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: SQLite storage with FTS5 sync, locking and store API"`

---

### Task 4: HTTP MCP server

**Files:**
- Create: `src/austin_power/server.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `config.Config`, `auth.check_bearer`, `db.open_db`, `db.ServerLock`, `db.*Error`, `store.*`, `store.ValidationError`.
- Produces:
  - `server.build_app(conn: apsw.Connection, token: str, *, port: int) -> ASGIApp` — `/mcp` (bearer) + `/health` (open).
  - `server.serve(cfg: Config) -> int` — exit code per spec §2.3.
  - Tool names exactly: `save`, `search`, `get`, `recent`, `forget`. Tool results are the dicts from `store` (structured output). Errors raise `mcp.server.mcpserver.exceptions.ToolError(message)` with messages from spec §2.6 error table.

- [ ] **Step 1: failing tests** (`tests/test_server.py`, `pytestmark = pytest.mark.kiwi`) using `httpx.AsyncClient(transport=httpx.ASGITransport(app))` inside an `anyio`-driven test. Because the MCP streamable HTTP app needs its lifespan (session manager), wrap with `asgi-lifespan`-style manual startup: use `app.router.lifespan_context(app)` from Starlette.

```python
import json, pytest, anyio, httpx
from austin_power import db, server

pytestmark = pytest.mark.kiwi
TOKEN = "t0ken"
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
     "MCP-Protocol-Version": "2025-06-18", "Host": "127.0.0.1:7760"}

def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}

@pytest.fixture
def app(tmp_path):
    return server.build_app(db.open_db(tmp_path / "m.db"), TOKEN, port=7760)

async def call(app, body, headers=H, auth=True):
    hdrs = dict(headers)
    if auth:
        hdrs["Authorization"] = f"Bearer {TOKEN}"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7760") as c:
            return await c.post("/mcp", json=body, headers=hdrs)

def tool(app, name, args):
    r = anyio.run(call, app, rpc("tools/call", {"name": name, "arguments": args}))
    assert r.status_code == 200, r.text
    return r.json()["result"]

def test_unauthorized(app):
    r = anyio.run(call, app, rpc("tools/list"), H, False)
    assert r.status_code == 401 and "www-authenticate" not in r.headers

def test_bad_host(app):
    r = anyio.run(call, app, rpc("tools/list"), {**H, "Host": "evil.example:7760"})
    assert r.status_code == 421

def test_health_open(app):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7760") as c:
            return await c.get("/health", headers={"Host": "127.0.0.1:7760"})
    r = anyio.run(go)
    assert r.status_code == 200 and r.json()["name"] == "austin-power" and "count" not in r.json()

def test_tools_listed(app):
    r = anyio.run(call, app, rpc("tools/list"))
    assert {t["name"] for t in r.json()["result"]["tools"]} == {"save", "search", "get", "recent", "forget"}

def test_call_without_initialize_roundtrip(app):
    res = tool(app, "save", {"title": "배포", "body": "배포 절차를 정리했다", "project": "p"})
    assert res["isError"] is False and res["structuredContent"]["action"] == "created"
    res = tool(app, "search", {"query": "배포가", "project": "p"})
    assert res["structuredContent"]["results"][0]["title"] == "배포"

def test_validation_error_is_tool_error(app):
    res = tool(app, "save", {"title": "", "body": "b"})
    assert res["isError"] is True and "invalid title" in res["content"][0]["text"]

def test_get_missing(app):
    res = tool(app, "get", {"id": 999})
    assert res["isError"] is True and "note 999 not found" in res["content"][0]["text"]
```

(If `structuredContent` wraps the dict as `{"result": {...}}`, fix `build_app` so tools return the dict unwrapped — declare return annotations as `dict[str, Any]` and pass `structured_output=True`; the test is the contract.)

- [ ] **Step 2: implement `server.py`**

```python
from __future__ import annotations
import errno, json, logging, socket, threading
from functools import partial
from typing import Any

import anyio, apsw, uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from austin_power import __version__, auth, db, store
from austin_power.config import Config, ConfigError, ensure_home, require_loopback

log = logging.getLogger("austin_power.server")

class _Bearer:
    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1") or None
            if not auth.check_bearer(header, self.token):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)

def build_app(conn: apsw.Connection, token: str, *, port: int):
    lock = threading.Lock()

    async def run(fn, *args, **kwargs):
        def work():
            with lock:
                return fn(conn, *args, **kwargs)
        try:
            return await anyio.to_thread.run_sync(work)
        except store.ValidationError as e:
            raise ToolError(str(e)) from None
        except apsw.BusyError:
            raise ToolError("storage busy, retry later") from None
        except (apsw.Error, db.TokenizerMismatchError) as e:
            log.exception("storage error")
            raise ToolError(f"storage error: {type(e).__name__}") from None

    srv = MCPServer(name="austin-power", version=__version__,
                    instructions="Long-term memory for coding agents. search before asking the user; save durable knowledge.")

    @srv.tool(description="Save a memory. Same (project, title) overwrites. kind examples: fact, decision, pattern, gotcha, workflow, session.", structured_output=True)
    async def save(title: str, body: str, project: str = "", kind: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        nid, action = await run(store.save, title=title, body=body, project=project, kind=kind, session_id=session_id)
        return {"id": nid, "action": action}

    @srv.tool(description="Full-text search (Korean-aware). Returns excerpts; use get for the full body.", structured_output=True)
    async def search(query: str, project: str | None = None, kind: str | None = None, limit: int = 10) -> dict[str, Any]:
        return await run(store.search, query, project=project, kind=kind, limit=limit)

    @srv.tool(description="Get one memory with its full body by id.", structured_output=True)
    async def get(id: int) -> dict[str, Any]:
        note = await run(store.get, id)
        if note is None:
            raise ToolError(f"note {id} not found")
        return note

    @srv.tool(description="Most recently updated memories, optionally for one project.", structured_output=True)
    async def recent(project: str | None = None, kind: str | None = None, limit: int = 10) -> dict[str, Any]:
        return {"results": await run(store.recent, project=project, kind=kind, limit=limit)}

    @srv.tool(description="Delete a memory by id.", structured_output=True)
    async def forget(id: int) -> dict[str, Any]:
        return {"id": id, "deleted": await run(store.forget, id)}

    @srv.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok", "name": "austin-power", "version": __version__})

    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    app = srv.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]))
    wrapped = _Bearer(app, token)
    wrapped.router = app.router  # tests use app.router.lifespan_context
    return wrapped

def serve(cfg: Config) -> int:
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        require_loopback(cfg.host)
    except ConfigError as e:
        log.error("%s", e); return 2
    ensure_home(cfg)
    token = auth.ensure_token(cfg.token_path)
    lock = db.ServerLock(cfg.lock_path)
    if not lock.acquire():
        log.error("another austin-power server is already running for %s", cfg.home); return 1
    try:
        try:
            conn = db.open_db(cfg.db_path, rebuild_allowed=True)
        except (apsw.Error, db.SchemaTooNewError, OSError) as e:
            log.error("cannot open database %s: %s", cfg.db_path, e); return 1
        family = socket.AF_INET6 if ":" in cfg.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((cfg.host, cfg.port))
        except OSError as e:
            sock.close()
            log.error("port %d is in use (%s)", cfg.port, errno.errorcode.get(e.errno, e)); return 1
        app = build_app(conn, token, port=cfg.port)
        log.info("listening on %s", cfg.mcp_url)
        uvicorn.Server(uvicorn.Config(app, log_level=cfg.log_level.lower(), lifespan="on")).run(sockets=[sock])
        conn.close()
        return 0
    finally:
        lock.release()
```

Notes: `SO_REUSEADDR` on macOS allows a second bind only when the other socket is in TIME_WAIT — an actively listening port still fails. If the tests reveal the `_Bearer.router` shim is awkward, expose `build_app(...) -> tuple[asgi, starlette_app]` instead and update tests accordingly.

Run `uv run pytest tests/test_server.py -v` → PASS.

- [ ] **Step 3: live smoke test** (`tests/test_server_live.py`, marked `kiwi`): start `serve()` in a subprocess (`uv run python -m austin_power serve --port <free port>` with `AUSTIN_POWER_HOME=tmp`) — wait for `/health` 200 (≤15 s) — assert a second `serve` exits 1 — POST `tools/call save` with urllib — terminate with SIGTERM — assert exit code 0. (Requires Task 5's CLI; if Task 5 is not done yet, invoke `python -c "from austin_power.server import serve; from austin_power.config import load_config; raise SystemExit(serve(load_config(port=P)))"`.)

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat: stateless HTTP MCP server with bearer auth"`

---

### Task 5: CLI + JSONL import

**Files:**
- Create: `src/austin_power/importer.py`, `tests/test_importer.py`, `tests/test_cli.py`
- Modify: `src/austin_power/cli.py` (replace placeholder), `src/austin_power/__main__.py` (unchanged unless needed)

**Interfaces:**
- Consumes: `config.load_config/ensure_home/require_loopback/ConfigError`, `auth.*`, `db.*`, `store.ImportRow/import_row/plan_import_row/normalize_kind/ValidationError`, `server.serve`, `hook.post_compact/session_start` (Task 6 — cli dispatches `hook` subcommands by importing `austin_power.hook` lazily inside the handler).
- Produces:
  - `importer.parse_line(line: str) -> ImportRow` (raises `ValueError` with reason)
  - `importer.run_import(cfg: Config, path: Path, *, dry_run: bool, out=sys.stdout) -> int`
  - `cli.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: failing importer tests** (`pytestmark = pytest.mark.kiwi` for tests that write)

```python
import json, pytest
from austin_power import importer, db, store
from austin_power.config import load_config

def cfg(tmp_path):
    return load_config(env={"AUSTIN_POWER_HOME": str(tmp_path / "h")})

def write(tmp_path, rows, raw=None):
    p = tmp_path / "in.jsonl"
    p.write_bytes(raw if raw is not None else "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode())
    return p

@pytest.mark.parametrize("value,expected", [
    ("2026-07-12T08:47:54.828Z", 1783846074), ("2026-07-12T08:47:54", 1783846074),
    (1783846074, 1783846074), (1783846074.9, 1783846074), (1783846074828, 1783846074),
])
def test_time_parsing(value, expected):
    row = importer.parse_line(json.dumps({"title": "t", "body": "b", "created_at": value}))
    assert row.created_at == expected and row.updated_at == expected

@pytest.mark.parametrize("line,reason", [
    ('{"title":"t"}', "body"), ("[1]", "object"), ("not json", "JSON"),
    ('{"title":"t","body":"b","created_at":20,"updated_at":10}', "updated_at"),
    ('{"title":"t","body":"b","created_at":"yesterday"}', "created_at"),
    ('{"title":"t","body":"b","created_at":-5}', "created_at"),
])
def test_parse_errors(line, reason):
    with pytest.raises(ValueError, match=reason):
        importer.parse_line(line)

@pytest.mark.kiwi
def test_import_counts_and_rerun(tmp_path, capsys):
    rows = [{"title": f"t{i}", "body": "본문", "created_at": 10, "updated_at": 10} for i in range(3)]
    p = write(tmp_path, rows + [{"title": "", "body": "x"}])
    c = cfg(tmp_path)
    assert importer.run_import(c, p, dry_run=False) == 1
    assert "created 3, updated 0, skipped 0, failed 1" in capsys.readouterr().out
    assert importer.run_import(c, write(tmp_path, rows), dry_run=False) == 0
    assert "created 0, updated 0, skipped 3, failed 0" in capsys.readouterr().out

@pytest.mark.kiwi
def test_import_older_is_skipped(tmp_path, capsys):
    c = cfg(tmp_path)
    conn = db.open_db(c.db_path); store.save(conn, title="t", body="new", now=100); conn.close()
    importer.run_import(c, write(tmp_path, [{"title": "t", "body": "old", "updated_at": 50}]), dry_run=False)
    conn = db.open_db(c.db_path)
    assert conn.execute("select body from note").fetchone()[0] == "new"

def test_dry_run_creates_nothing(tmp_path, capsys):
    c = cfg(tmp_path)
    assert importer.run_import(c, write(tmp_path, [{"title": "t", "body": "b"}]), dry_run=True) == 0
    assert "created 1" in capsys.readouterr().out and not c.db_path.exists()

def test_missing_file(tmp_path):
    assert importer.run_import(cfg(tmp_path), tmp_path / "nope.jsonl", dry_run=False) == 2

@pytest.mark.kiwi
def test_import_while_locked_refuses_rebuild(tmp_path, capsys):
    c = cfg(tmp_path)
    conn = db.open_db(c.db_path); conn.execute("update meta set value='old' where key='tokenizer_sig'"); conn.close()
    lock = db.ServerLock(c.lock_path); assert lock.acquire()
    try:
        assert importer.run_import(c, write(tmp_path, [{"title": "t", "body": "b"}]), dry_run=False) == 1
        assert "restart" in capsys.readouterr().out
    finally:
        lock.release()
```

(Expected epoch for `2026-07-12T08:47:54Z` must be computed in the test with `datetime(2026,7,12,8,47,54,tzinfo=timezone.utc).timestamp()` rather than trusting the literal above — replace the literals with that computation when writing the file.)

- [ ] **Step 2: implement `importer.py`**

```python
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

import apsw
from austin_power import db, store
from austin_power.config import Config, ensure_home

MAX_TS = 253402300799  # 9999-12-31T23:59:59Z
BATCH_ROWS, BATCH_SECONDS = 200, 1.0

def _ts(field: str, value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}: not a timestamp")
    if isinstance(value, (int, float)):
        n = float(value)
        if n >= 1e11:
            n /= 1000
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{field}: not ISO 8601 or epoch") from None
        n = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    else:
        raise ValueError(f"{field}: not a timestamp")
    n = int(n)  # floor for non-negative values
    if not 0 <= n <= MAX_TS:
        raise ValueError(f"{field}: out of range")
    return n

def parse_line(line: str) -> store.ImportRow:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        raise ValueError("not valid JSON") from None
    if not isinstance(obj, dict):
        raise ValueError("line must be a JSON object")
    try:
        title, body, project, kind, sid = store._clean(obj.get("title"), obj.get("body"), obj.get("project", ""),
                                                      obj.get("kind"), obj.get("session_id"))
    except store.ValidationError as e:
        raise ValueError(str(e)) from None
    c, u = _ts("created_at", obj.get("created_at")), _ts("updated_at", obj.get("updated_at"))
    c = u if c is None else c
    u = c if u is None else u
    if c is not None and u < c:
        raise ValueError("updated_at is earlier than created_at")
    return store.ImportRow(title, body, project, kind, sid, c, u)

def _read_rows(path: Path):
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                yield n, None, "not valid UTF-8"; continue
            if not line:
                continue
            try:
                yield n, parse_line(line), None
            except ValueError as e:
                yield n, None, str(e)

def run_import(cfg: Config, path: Path, *, dry_run: bool, out=sys.stdout) -> int:
    path = Path(path)
    if not path.is_file():
        print(f"error: cannot read {path}", file=out); return 2
    counts = {"created": 0, "updated": 0, "skipped": 0}
    failures: list[str] = []
    committed = 0

    def report():
        print(f"created {counts['created']}, updated {counts['updated']}, skipped {counts['skipped']}, failed {len(failures)}", file=out)
        for f in failures[:50]:
            print(f, file=out)
        if len(failures) > 50:
            print(f"... and {len(failures) - 50} more", file=out)

    if dry_run:
        try:
            conn = db.open_db_readonly(cfg.db_path)
        except db.SchemaTooNewError as e:
            print(f"error: {e}", file=out); return 1
        for n, row, err in _read_rows(path):
            if err:
                failures.append(f"line {n}: {err}")
            else:
                counts[store.plan_import_row(conn, row)] += 1
        report()
        return 1 if failures else 0

    ensure_home(cfg)
    lock = db.ServerLock(cfg.lock_path)
    have_lock = lock.acquire()
    try:
        try:
            conn = db.open_db(cfg.db_path, rebuild_allowed=have_lock)
        except (db.TokenizerMismatchError, db.SchemaTooNewError) as e:
            print(f"error: {e}", file=out); return 1
        batch: list = []

        def flush():
            nonlocal committed
            for attempt in (1, 2):
                try:
                    with db.write_txn(conn):
                        results = [store.import_row(conn, r) for _, r in batch]
                    break
                except apsw.BusyError:
                    if attempt == 2:
                        raise
            for r in results:
                counts[r] += 1
            committed += len(batch)
            batch.clear()

        started = time.monotonic()
        try:
            for n, row, err in _read_rows(path):
                if err:
                    failures.append(f"line {n}: {err}"); continue
                batch.append((n, row))
                if len(batch) >= BATCH_ROWS or time.monotonic() - started >= BATCH_SECONDS:
                    flush(); started = time.monotonic()
            if batch:
                flush()
        except (apsw.Error, db.TokenizerMismatchError, OSError) as e:
            print(f"committed {committed} rows before failure: {type(e).__name__}: {e}", file=out)
            report()
            return 1
        report()
        return 1 if failures else 0
    finally:
        if have_lock:
            lock.release()
```

Note: `store._clean` is used intentionally so import and save share one validator; rename it to `store.clean_fields` (public) and update Task 3 code if the reviewer objects to the private import.

- [ ] **Step 3: failing CLI tests** (`tests/test_cli.py`)

```python
import json, subprocess, sys
from austin_power.cli import main

def run(argv, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    code = main(argv)
    return code, capsys.readouterr()

def test_version(tmp_path, capsys, monkeypatch):
    code, out = run(["--version"], tmp_path, capsys, monkeypatch)  # argparse exits
    # argparse's version action raises SystemExit(0); main must catch it and return 0
    assert code == 0 and "0.1.0" in out.out

def test_token_prints_and_is_stable(tmp_path, capsys, monkeypatch):
    c1, o1 = run(["token"], tmp_path, capsys, monkeypatch)
    c2, o2 = run(["token"], tmp_path, capsys, monkeypatch)
    assert c1 == c2 == 0 and o1.out.strip() == o2.out.strip() and len(o1.out.strip()) >= 40

def test_setup_claude(tmp_path, capsys, monkeypatch):
    code, out = run(["setup", "claude", "--port", "7799"], tmp_path, capsys, monkeypatch)
    assert code == 0
    assert "claude mcp add --transport http --scope user austin-power http://127.0.0.1:7799/mcp --header \"Authorization: Bearer " in out.out
    assert "token" in out.err.lower()

def test_setup_codex(tmp_path, capsys, monkeypatch):
    code, out = run(["setup", "codex"], tmp_path, capsys, monkeypatch)
    assert "codex mcp add austin-power --url http://127.0.0.1:7760/mcp --bearer-token-env-var AUSTIN_POWER_TOKEN" in out.out
    assert 'export AUSTIN_POWER_TOKEN="$(austin-power token)"' in out.out

def test_setup_hooks_is_valid_json(tmp_path, capsys, monkeypatch):
    code, out = run(["setup", "hooks"], tmp_path, capsys, monkeypatch)
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
    r = subprocess.run([sys.executable, "-m", "austin_power", "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and "0.1.0" in r.stdout
```

- [ ] **Step 4: implement `cli.py`**

```python
from __future__ import annotations
import argparse, json, sys, urllib.request
from pathlib import Path

from austin_power import __version__
from austin_power.config import ConfigError, ensure_home, load_config, require_loopback

HOOKS_SNIPPET = {"hooks": {
    "PostCompact": [{"hooks": [{"type": "command", "command": "austin-power hook post-compact", "timeout": 30}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "austin-power hook session-start", "timeout": 10}]}],
}}

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="austin-power", description="Lightweight Korean-aware memory server for coding agents")
    p.add_argument("--version", action="version", version=f"austin-power {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    def addr(sp):
        sp.add_argument("--host"); sp.add_argument("--port")
    addr(sub.add_parser("serve", help="run the server in the foreground"))
    addr(sub.add_parser("status", help="check whether the server is running"))
    t = sub.add_parser("token", help="print (or rotate) the bearer token"); t.add_argument("--rotate", action="store_true")
    s = sub.add_parser("setup", help="print registration snippets (never edits other tools' config)")
    ss = s.add_subparsers(dest="target", required=True)
    addr(ss.add_parser("claude")); addr(ss.add_parser("codex")); ss.add_parser("hooks")
    i = sub.add_parser("import", help="import memories from JSONL"); i.add_argument("file"); i.add_argument("--dry-run", action="store_true")
    h = sub.add_parser("hook", help="Claude Code hook entry points (read JSON on stdin)")
    h.add_argument("event", choices=["post-compact", "session-start"])
    return p

def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.cmd == "hook":
        from austin_power import hook
        return hook.main(args.event)
    try:
        cfg = load_config(host=getattr(args, "host", None), port=getattr(args, "port", None))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    if args.cmd == "serve":
        from austin_power.server import serve
        return serve(cfg)
    if args.cmd == "status":
        try:
            with urllib.request.urlopen(cfg.base_url + "/health", timeout=2) as r:
                info = json.load(r)
            print(f"running: {cfg.base_url} (v{info.get('version', '?')})"); return 0
        except Exception:
            print(f"not running: {cfg.base_url}"); return 1
    from austin_power import auth
    if args.cmd == "token":
        ensure_home(cfg)
        if args.rotate:
            print(auth.rotate_token(cfg.token_path))
            print("token rotated: restart the server and re-register clients (austin-power setup claude|codex)", file=sys.stderr)
        else:
            print(auth.ensure_token(cfg.token_path))
        return 0
    if args.cmd == "setup":
        if args.target == "hooks":
            print(json.dumps(HOOKS_SNIPPET, indent=2)); return 0
        try:
            require_loopback(cfg.host)
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr); return 2
        ensure_home(cfg)
        token = auth.ensure_token(cfg.token_path)
        if args.target == "claude":
            print(f'claude mcp add --transport http --scope user austin-power {cfg.mcp_url} --header "Authorization: Bearer {token}"')
            print("warning: the command above contains your token", file=sys.stderr)
        else:
            print(f"codex mcp add austin-power --url {cfg.mcp_url} --bearer-token-env-var AUSTIN_POWER_TOKEN")
            print('# add to your shell profile:\nexport AUSTIN_POWER_TOKEN="$(austin-power token)"')
        return 0
    if args.cmd == "import":
        from austin_power.importer import run_import
        return run_import(cfg, Path(args.file), dry_run=args.dry_run)
    return 2
```

Until Task 6 lands, `hook` import fails only when the `hook` subcommand is used; tests for it live in Task 6.

Run `uv run pytest tests/test_importer.py tests/test_cli.py -v` → PASS. Also run Task 4 Step 3 live smoke test via the CLI.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: CLI and JSONL import"`

---

### Task 6: Claude Code hooks

**Files:**
- Create: `src/austin_power/hook.py`, `tests/test_hook.py`

**Interfaces:**
- Consumes: `config.load_config`, `auth.read_token/TokenError` (auth is stdlib-only ✓), lazily `db.open_db`, `db.ServerLock`, `store.save` (fallback only).
- Produces: `hook.main(event: str, *, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, env=None) -> int` (always 0); `hook.resolve_project(cwd: str, env) -> str`; `hook.call_tool(cfg, token, name, args, timeout=5.0) -> dict` raising `hook.Unreachable`, `hook.ServerError`, `TimeoutError`.

- [ ] **Step 1: failing tests** (`tests/test_hook.py`)

```python
import io, json, sys, subprocess
import pytest
from austin_power import hook, db

def run(event, payload, tmp_path, monkeypatch, raw=None):
    env = {"AUSTIN_POWER_HOME": str(tmp_path / "h"), "AUSTIN_POWER_PORT": "1", "AUSTIN_POWER_PROJECT": "proj"}
    out, err = io.StringIO(), io.StringIO()
    code = hook.main(event, stdin=io.StringIO(raw if raw is not None else json.dumps(payload)), stdout=out, stderr=err, env=env)
    return code, out.getvalue(), err.getvalue()

def test_import_is_light():
    code = "import sys, austin_power.hook; bad=[m for m in ('kiwipiepy','apsw','mcp','uvicorn','anyio') if m in sys.modules]; print(bad)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.stdout.strip() == "[]"

@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"cwd": 3}'])
def test_malformed_stdin_is_fail_open(raw, tmp_path, monkeypatch):
    for ev in ("post-compact", "session-start"):
        code, out, _ = run(ev, None, tmp_path, monkeypatch, raw=raw)
        assert code == 0 and out == ""

def test_post_compact_empty_summary_noop(tmp_path, monkeypatch):
    code, out, _ = run("post-compact", {"session_id": "s", "cwd": str(tmp_path), "compact_summary": ""}, tmp_path, monkeypatch)
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
```

Also add a live test (in `tests/test_server_live.py`, extending Task 4's fixture): with the server running, `post-compact` saves via HTTP (row visible through `search`), and `session-start` prints valid JSON whose `hookSpecificOutput.additionalContext` contains the saved title.

- [ ] **Step 2: implement `hook.py`**

```python
from __future__ import annotations
import json, os, subprocess, sys, urllib.error, urllib.request
from pathlib import Path

from austin_power import auth
from austin_power.config import ConfigError, load_config

SUMMARY_MAX, SUMMARY_KEEP = 32000, 31000

class Unreachable(Exception): ...
class ServerError(Exception): ...

def resolve_project(cwd: str, env) -> str:
    if v := (env.get("AUSTIN_POWER_PROJECT") or "").strip():
        return v[:100]
    if not cwd:
        return ""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).name[:100]
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name[:100]

def call_tool(cfg, token: str, name: str, args: dict, timeout: float = 5.0) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(cfg.mcp_url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        raise ServerError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            raise Unreachable(str(e.reason)) from None
        if isinstance(e.reason, TimeoutError):
            raise TimeoutError() from None
        raise ServerError(str(e.reason)) from None
    result = payload.get("result") or {}
    if payload.get("error") or result.get("isError"):
        raise ServerError(json.dumps(payload.get("error") or result.get("content"), ensure_ascii=False)[:300])
    sc = result.get("structuredContent") or {}
    return sc["result"] if set(sc) == {"result"} and isinstance(sc["result"], dict) else sc

def _fallback_save(cfg, fields: dict, err) -> None:
    from austin_power import db, store  # heavy imports only here
    lock = db.ServerLock(cfg.lock_path)
    if not lock.acquire():
        print(f"austin-power: server running but unreachable at {cfg.mcp_url} — check AUSTIN_POWER_HOST/PORT", file=err)
        return
    try:
        conn = db.open_db(cfg.db_path, busy_timeout=20000, rebuild_allowed=True)
        try:
            store.save(conn, **fields)
        finally:
            conn.close()
    finally:
        lock.release()

def format_context(project: str, items: list[dict], cap: int) -> str:
    lines = [f"austin-power: recent memories for {project} (use the search/get tools for more)"]
    size = len(lines[0])
    for it in items:
        line = f"- [{it['kind']}] {it['title']} (#{it['id']}, {it['updated_at'][:10]}): {it['preview']}".replace("\n", " ")
        if size + 1 + len(line) > cap:
            break
        lines.append(line); size += 1 + len(line)
    return "\n".join(lines) if len(lines) > 1 else ""

def _post_compact(data: dict, cfg, env, out, err) -> None:
    summary = data.get("compact_summary")
    sid = data.get("session_id")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(sid, str) or not sid:
        return
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_KEEP] + "\n…(truncated)"
    fields = {"title": "session " + sid[:180], "body": summary, "kind": "session",
              "project": resolve_project(data.get("cwd") or "", env), "session_id": sid[:200]}
    try:
        token = auth.read_token(cfg.token_path)
    except auth.TokenError:
        _fallback_save(cfg, fields, err); return
    try:
        call_tool(cfg, token, "save", fields)
    except Unreachable:
        _fallback_save(cfg, fields, err)
    except TimeoutError:
        print("austin-power: server timed out; summary not saved", file=err)
    except ServerError as e:
        print(f"austin-power: save failed: {e}", file=err)

def _session_start(data: dict, cfg, env, out, err) -> None:
    if data.get("source") == "compact":
        return
    project = resolve_project(data.get("cwd") or "", env)
    if not project:
        return
    try:
        token = auth.read_token(cfg.token_path)
        res = call_tool(cfg, token, "recent", {"project": project, "limit": 8})
    except (auth.TokenError, Unreachable, ServerError, TimeoutError):
        return
    text = format_context(project, res.get("results", []), cfg.inject_chars)
    if text:
        out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))

def main(event: str, *, stdin=None, stdout=None, stderr=None, env=None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    env = os.environ if env is None else env
    try:
        data = json.loads(stdin.read() or "null")
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        if not isinstance(data.get("cwd", ""), str):
            raise ValueError("cwd must be a string")
        cfg = load_config(env=env)
        (_post_compact if event == "post-compact" else _session_start)(data, cfg, env, stdout, stderr)
    except (ValueError, ConfigError) as e:
        print(f"austin-power hook: ignored invalid input: {e}", file=stderr)
    except Exception as e:  # fail-open: never break the user's session
        print(f"austin-power hook: {type(e).__name__}: {e}", file=stderr)
    return 0
```

Run `uv run pytest tests/test_hook.py -v` → PASS (port `1` guarantees connection refused).

- [ ] **Step 3: Commit** — `git add -A && git commit -m "feat: PostCompact distillation and SessionStart injection hooks"`

---

### Task 7: agentmemory converter (contrib)

**Files:**
- Create: `contrib/agentmemory_to_jsonl.py`, `contrib/README.md`, `tests/test_contrib_agentmemory.py`, `tests/fixtures/agentmemory/mem%3Amemories.bin`, `tests/fixtures/agentmemory/mem%3Asessions.bin`

**Interfaces:**
- Produces: `convert(store_dir: Path) -> tuple[list[dict], list[str]]` (rows, skip messages) and CLI `python contrib/agentmemory_to_jsonl.py [STORE_DIR] [-o FILE]`. Stdlib only.

- [ ] **Step 1: fixtures** — synthetic data only (never copy real memories):

`mem%3Amemories.bin`:
```json
{
 "mem_1": {"id":"mem_1","title":"[wiki:x] ---\ntitle: 견적 시스템\nproject: shop\n---","content":"[wiki:x] ---\ntitle: 견적 시스템\nproject: shop\n---\n본문 하나","type":"Architecture","concepts":["quote","견적"],"files":["a.py"],"isLatest":true,"sessionIds":[],"createdAt":"2026-07-12T08:47:54.828Z","updatedAt":"2026-07-12T08:47:54.828Z"},
 "mem_2": {"id":"mem_2","title":"배포 절차","content":"배포 본문","type":"workflow","concepts":[],"files":[],"isLatest":true,"sessionIds":["s1","s2","s3"],"createdAt":"2026-07-13T00:00:00Z","updatedAt":"2026-07-14T00:00:00Z"},
 "mem_3": {"id":"mem_3","title":"배포 절차","content":"같은 제목 다른 본문","type":"workflow","concepts":[],"files":[],"isLatest":true,"sessionIds":["s1"],"createdAt":"2026-07-13T00:00:00Z","updatedAt":"2026-07-13T00:00:00Z"},
 "mem_4": {"id":"mem_4","title":"old","content":"superseded","type":"fact","isLatest":false,"sessionIds":[],"createdAt":"2026-07-01T00:00:00Z","updatedAt":"2026-07-01T00:00:00Z"},
 "mem_5": {"id":"mem_5","title":"broken"}
}
```
`mem%3Asessions.bin`:
```json
{"s1":{"id":"s1","project":"api","cwd":"/x/api"},"s2":{"id":"s2","project":"web","cwd":"/x/web"},"s3":{"id":"s3","project":"web","cwd":"/x/web"}}
```

- [ ] **Step 2: failing tests**

```python
import importlib.util, json, sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "agentmemory"
spec = importlib.util.spec_from_file_location("am", Path(__file__).parents[1] / "contrib" / "agentmemory_to_jsonl.py")
am = importlib.util.module_from_spec(spec); spec.loader.exec_module(am)

def test_convert():
    rows, skips = am.convert(FIX)
    by = {(r["project"], r["title"]): r for r in rows}
    assert len(rows) == 3 and any("mem_5" in s for s in skips)
    q = by[("shop", "견적 시스템")]
    assert q["kind"] == "architecture" and q["body"].endswith("\n\nconcepts: quote, 견적\nfiles: a.py")
    assert by[("web", "배포 절차")]["updated_at"] == "2026-07-14T00:00:00Z"   # most frequent project web(2) over api(1)
    assert ("api", "배포 절차") in by                                         # mem_3 → api, no collision
    assert all(r["title"] != "old" for r in rows)

def test_collision_suffix_and_limits(tmp_path):
    mems = {f"m{i}": {"id": f"m{i}", "title": "x" * 250, "content": "c", "type": "Weird Type!", "isLatest": True,
                      "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"} for i in range(2)}
    (tmp_path / "mem%3Amemories.bin").write_text(json.dumps(mems))
    rows, _ = am.convert(tmp_path)
    titles = sorted(r["title"] for r in rows)
    assert len(titles[0]) == 200 and titles[1].endswith(" (2)") and len(titles[1]) == 200
    assert rows[0]["kind"] == "weird-type"
```

(Row order follows sorted memory ids, so collision numbering is deterministic.)

- [ ] **Step 3: implement `contrib/agentmemory_to_jsonl.py`**

```python
#!/usr/bin/env python3
"""Convert an agentmemory KV store into austin-power JSONL (stdlib only).

usage: python agentmemory_to_jsonl.py [STORE_DIR] [-o OUT.jsonl]
then:  austin-power import OUT.jsonl
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from pathlib import Path

TITLE_MAX, BODY_MAX, BODY_KEEP = 200, 32000, 31000

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.JSONDecoder().raw_decode(raw)[0]  # tolerate trailing garbage after the object

def _kind(t) -> str:
    k = re.sub(r"[^a-z0-9_-]", "-", str(t or "").lower())
    k = re.sub(r"-{2,}", "-", k).strip("-_")[:32]
    return k or "fact"

def _frontmatter(text: str) -> dict:
    m = re.match(r"^\s*(?:\[[^\]\n]*\]\s*)?---\s*\n(.*?)\n---\s*(?:\n|$)", text or "", re.S)
    out: dict = {}
    if m:
        for line in m.group(1).splitlines():
            key, sep, val = line.strip().partition(":")
            if sep and key.strip() and key.strip() not in out and val.strip():
                out[key.strip()] = val.strip().strip("'\"")
    return out

def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and line != "---" and not re.fullmatch(r"\[[^\]]*\]\s*---", line):
            return line
    return ""

def convert(store_dir: Path) -> tuple[list[dict], list[str]]:
    mems = _load(Path(store_dir) / "mem%3Amemories.bin")
    sessions = _load(Path(store_dir) / "mem%3Asessions.bin")
    rows, skips, seen = [], [], Counter()
    for mid in sorted(mems):
        m = mems[mid]
        try:
            if not isinstance(m, dict) or m.get("isLatest") is False:
                continue
            content = m["content"]
            fm = _frontmatter(content) or _frontmatter(m.get("title", ""))
            projects = Counter(sessions.get(s, {}).get("project") for s in m.get("sessionIds") or [])
            projects.pop(None, None); projects.pop("", None)
            project = (sorted(projects.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if projects else fm.get("project", ""))[:100]
            title = (fm.get("title") or _first_line(m.get("title", "")) or _first_line(content)).strip()
            if not title or not content.strip():
                raise ValueError("empty title or content")
            base = title[:TITLE_MAX]
            seen[(project, base)] += 1
            n = seen[(project, base)]
            if n > 1:
                suffix = f" ({n})"
                base = title[:TITLE_MAX - len(suffix)] + suffix
            body = content
            if m.get("concepts"):
                body += "\n\nconcepts: " + ", ".join(map(str, m["concepts"]))
            if m.get("files"):
                body += ("\n" if m.get("concepts") else "\n\n") + "files: " + ", ".join(map(str, m["files"]))
            if len(body) > BODY_MAX:
                body = body[:BODY_KEEP] + "\n…(truncated)"
            rows.append({"title": base, "body": body, "project": project, "kind": _kind(m.get("type")),
                         "created_at": m.get("createdAt"), "updated_at": m.get("updatedAt")})
        except (KeyError, TypeError, ValueError) as e:
            skips.append(f"skip {mid}: {type(e).__name__}: {e}")
    return rows, skips

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("store", nargs="?", default=str(Path.home() / ".agentmemory" / "data" / "state_store.db"))
    p.add_argument("-o", "--output")
    a = p.parse_args(argv)
    rows, skips = convert(Path(a.store).expanduser())
    out = open(a.output, "w", encoding="utf-8") if a.output else sys.stdout
    try:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    finally:
        if a.output:
            out.close()
    for s in skips:
        print(s, file=sys.stderr)
    print(f"converted {len(rows)}, skipped {len(skips)}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

Note on collision numbering: the test expects the *second* occurrence to get ` (2)`; `seen` counts per `(project, base-title)`.

`contrib/README.md`: 5 lines — purpose, usage, "not part of the core package; format reverse-engineered from a local store, may break with future agentmemory versions".

Run `uv run pytest tests/test_contrib_agentmemory.py -v` → PASS.

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat(contrib): agentmemory to JSONL converter"`

---

### Task 8: Docs, examples, CI

**Files:**
- Create: `README.md` (Korean), `README.en.md`, `docs/design.md`, `examples/launchd/io.github.yuminfa.austin-power.plist`, `examples/systemd/austin-power.service`, `.github/workflows/ci.yml`

**Interfaces:** none (docs). Content must match actual CLI output/flags from Tasks 5–6.

- [ ] **Step 1: README.md (Korean)** sections, in this order:
  1. 제목 + 한 줄: "코딩 에이전트를 위한 가볍고 한국어에 강한 기억 저장소 (MCP)".
  2. 왜 만들었나 — 표: 상주 프로세스·세션당 프로세스·인덱스 방식·재색인·한국어 검색, "기존 범용 메모리 서버 사용 경험" 대비 austin-power. 수치는 README에 **실측값만**(RSS는 Task 9 측정값을 넣고, 측정 전이면 해당 행을 비움). 특정 프로젝트를 비방하지 않는다 — "agentmemory에서 옮겨 오기" 절에서만 이름을 쓴다.
  3. 특징 불릿 5개: 단일 SQLite 파일 / FTS5 증분 색인(재색인 없음) / Kiwi 형태소 + 식별자 원형 보존 / 서버는 LLM 호출 없음 / 압축 요약 자동 저장.
  4. 한국어 검색 예시 표: `인덱스를 다시 만들었다` 저장 → `인덱스가`, `만들다`, `note_fts`, `fts` 로 검색되는지.
  5. 설치·실행: `uv tool install git+https://github.com/yuminFa/austin-power` → `austin-power serve` → 상주(launchd/systemd 예시 링크).
  6. 등록: `austin-power setup claude` / `setup codex` / `setup hooks` 출력 예시와 붙여넣는 위치.
  7. 도구 5개 표.
  8. 설정 표(환경변수·기본값).
  9. 데이터·백업: 위치, `sqlite3 memory.db ".backup backup.db"`, WAL 주의, 외부 도구로 쓰기 불가, 세션 요약에 비밀값이 섞일 수 있음 → `forget`.
  10. agentmemory에서 옮겨 오기: `python contrib/agentmemory_to_jsonl.py -o am.jsonl && austin-power import am.jsonl`.
  11. 한계(v0.1): 로컬 전용, SessionEnd 증류 없음, Windows 미검증.
  12. 라이선스 MIT, 설계 문서 링크.
- [ ] **Step 2: README.en.md** — same structure, condensed.
- [ ] **Step 3: docs/design.md** — public design doc: problem, principles (single file, no LLM on server, no auto-capture, stateless HTTP), architecture diagram (mermaid), tokenizer rules, schema, hooks, deviations table (spec §1.1 wording, no personal details).
- [ ] **Step 4: examples** — launchd plist (`Label io.github.yuminfa.austin-power`, `ProgramArguments` `["/ABSOLUTE/PATH/TO/austin-power","serve"]` with XML comment "replace with `which austin-power`", `RunAtLoad true`, `KeepAlive true`, `StandardErrorPath` `/tmp/austin-power.log`); systemd user unit (`ExecStart=%h/.local/bin/austin-power serve`, `Restart=on-failure`, `WantedBy=default.target`) with install comments (`systemctl --user enable --now austin-power`).
- [ ] **Step 5: CI** `.github/workflows/ci.yml` — matrix os `[ubuntu-latest, macos-latest]` × python `["3.11", "3.13"]`; steps: checkout, `astral-sh/setup-uv@v6`, `uv sync --python ${{ matrix.python }}`, `uv run ruff check`, `uv run pytest -q`.
- [ ] **Step 6: portability check** — grep the tree for the maintainer's personal identifiers using a local, uncommitted deny-list: `grep -rniF -f "$AUSTIN_POWER_DENYLIST" --exclude-dir=.git --exclude-dir=.venv .` must print nothing (the deny-list file itself lives outside the repo).
- [ ] **Step 7: Commit** — `git add -A && git commit -m "docs: README, design doc, service examples, CI"`

---

### Task 9: Measurement & final verification (no new features)

**Files:**
- Create: `scripts/measure_rss.py`
- Modify: `README.md`, `README.en.md` (fill measured numbers)

- [ ] **Step 1:** `scripts/measure_rss.py` — starts `austin-power serve` on a free port with a temp home, waits for health, reads RSS (`ps -o rss= -p PID`), performs 1,000 `save`/`search` calls via urllib (alternating), reads RSS again, prints `startup_rss_mb`, `after_1000_rss_mb`, `growth_pct`. Acceptance (spec U8): `growth_pct <= 20`.
- [ ] **Step 2:** run it, write numbers into README "왜" table and U8 line in spec (`[x]` with measured values).
- [ ] **Step 3:** `uv run ruff check && uv run pytest -q` once.
- [ ] **Step 4: Commit** — `git commit -am "docs: measured memory footprint"`
