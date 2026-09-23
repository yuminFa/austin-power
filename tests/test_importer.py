import json
from datetime import UTC, datetime

import pytest

from austin_power import db, importer, store
from austin_power.config import load_config


def cfg(tmp_path):
    return load_config(env={"AUSTIN_POWER_HOME": str(tmp_path / "h")})


def write(tmp_path, rows, raw=None):
    p = tmp_path / "in.jsonl"
    p.write_bytes(raw if raw is not None else "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode())
    return p


_EPOCH = int(datetime(2026, 7, 12, 8, 47, 54, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-07-12T08:47:54.828Z", _EPOCH),
        ("2026-07-12T08:47:54", _EPOCH),
        (_EPOCH, _EPOCH),
        (_EPOCH + 0.9, _EPOCH),
        (_EPOCH * 1000, _EPOCH),
    ],
)
def test_time_parsing(value, expected):
    row = importer.parse_line(json.dumps({"title": "t", "body": "b", "created_at": value}))
    assert row.created_at == expected and row.updated_at == expected


@pytest.mark.parametrize(
    "line,reason",
    [
        ('{"title":"t"}', "body"),
        ("[1]", "object"),
        ("not json", "JSON"),
        ('{"title":"t","body":"b","created_at":20,"updated_at":10}', "updated_at"),
        ('{"title":"t","body":"b","created_at":"yesterday"}', "created_at"),
        ('{"title":"t","body":"b","created_at":-5}', "created_at"),
    ],
)
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
    conn = db.open_db(c.db_path)
    store.save(conn, title="t", body="new", now=100)
    conn.close()
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
    conn = db.open_db(c.db_path)
    conn.execute("update meta set value='old' where key='tokenizer_sig'")
    conn.close()
    lock = db.ServerLock(c.lock_path)
    assert lock.acquire()
    try:
        assert importer.run_import(c, write(tmp_path, [{"title": "t", "body": "b"}]), dry_run=False) == 1
        assert "restart" in capsys.readouterr().out
    finally:
        lock.release()
