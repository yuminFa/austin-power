from __future__ import annotations

import logging
import sys
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
    conn.set_busy_timeout(busy_timeout)
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
                    f"tokenizer changed ({old} -> {sig}) while another austin-power server is running; restart the server"
                )
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
        fh = open(self.path, "a+b")  # noqa: SIM115 - handle is kept on self for release() later
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
