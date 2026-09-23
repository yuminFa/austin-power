from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

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
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_fields(title, body, project, kind, session_id):
    """Validate and normalize the free-form fields shared by save/import.

    Public so importer (T5) can validate rows before writing.
    """
    return (
        _text("title", title, 1, TITLE_MAX),
        _text("body", body, 1, BODY_MAX),
        _text("project", "" if project is None else project, 0, PROJECT_MAX),
        None if kind is None else normalize_kind(kind),
        None if session_id is None else _text("session_id", session_id, 0, SESSION_MAX),
    )


def _existing(conn, project, title):
    return conn.execute(
        "SELECT id, body, kind, session_id, created_at, updated_at FROM note WHERE project=? AND title=?",
        (project, title),
    ).fetchone()


def save(conn, *, title, body, project="", kind=None, session_id=None, now=None) -> tuple[int, str]:
    title, body, project, kind, session_id = clean_fields(title, body, project, kind, session_id)
    now = int(time.time()) if now is None else int(now)
    with db.write_txn(conn):
        row = _existing(conn, project, title)
        if row is None:
            (nid,) = conn.execute(
                "INSERT INTO note(project, kind, title, body, session_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?) RETURNING id",
                (project, kind or "fact", title, body, session_id, now, now),
            ).fetchone()
            return nid, "created"
        conn.execute(
            "UPDATE note SET body=?, kind=coalesce(?, kind), session_id=coalesce(?, session_id), updated_at=? WHERE id=?",
            (body, kind, session_id, now, row[0]),
        )
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
    eff_kind = row.kind if row.kind is not None else kind
    eff_sid = row.session_id if row.session_id is not None else sid
    if row.updated_at == upd and row.body == body and eff_kind == kind and eff_sid == sid:
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
        conn.execute(
            "INSERT INTO note(project, kind, title, body, session_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (row.project, row.kind or "fact", row.title, row.body, row.session_id, c, u),
        )
    elif action == "updated":
        conn.execute(
            "UPDATE note SET body=?, kind=coalesce(?, kind), session_id=coalesce(?, session_id), updated_at=? WHERE id=?",
            (row.body, row.kind, row.session_id, row.updated_at, existing[0]),
        )
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
    # Lowercase before quoting so bareword FTS5 keywords (AND/OR/NOT/NEAR are
    # case-sensitive operators) never survive as operators; the "kiwi"
    # tokenizer lowercases at index time too, so this never changes matching.
    quoted = [apsw.fts5query.quote(p.lower()) for p in parts]
    attempts = [("all", " ".join(quoted))] + ([("any", " OR ".join(quoted))] if len(quoted) > 1 else [])
    for label, expr in attempts:
        rows = conn.execute(_SEARCH_SQL, (expr, project, project, kind, kind, limit)).fetchall()
        if rows:
            return {
                "match": label,
                "results": [
                    {"id": r[0], "project": r[1], "kind": r[2], "title": r[3], "excerpt": r[4], "updated_at": iso(r[5])}
                    for r in rows
                ],
            }
    return {"match": "none", "results": []}


def get(conn, note_id) -> dict | None:
    r = conn.execute(
        "SELECT id, project, kind, title, body, session_id, created_at, updated_at FROM note WHERE id=?",
        (_id(note_id),),
    ).fetchone()
    if r is None:
        return None
    return {
        "id": r[0],
        "project": r[1],
        "kind": r[2],
        "title": r[3],
        "body": r[4],
        "session_id": r[5],
        "created_at": iso(r[6]),
        "updated_at": iso(r[7]),
    }


def recent(conn, *, project=None, kind=None, limit=10) -> list[dict]:
    project = None if project is None else _text("project", project, 0, PROJECT_MAX)
    kind = None if kind is None else normalize_kind(kind)
    rows = conn.execute(
        "SELECT id, project, kind, title, body, updated_at FROM note "
        "WHERE (? IS NULL OR project=?) AND (? IS NULL OR kind=?) ORDER BY updated_at DESC, id DESC LIMIT ?",
        (project, project, kind, kind, _limit(limit)),
    ).fetchall()
    return [
        {
            "id": r[0],
            "project": r[1],
            "kind": r[2],
            "title": r[3],
            "preview": r[4] if len(r[4]) <= PREVIEW else r[4][:PREVIEW] + "…",
            "updated_at": iso(r[5]),
        }
        for r in rows
    ]


def forget(conn, note_id) -> bool:
    nid = _id(note_id)
    with db.write_txn(conn):
        return conn.execute("DELETE FROM note WHERE id=? RETURNING id", (nid,)).fetchone() is not None
