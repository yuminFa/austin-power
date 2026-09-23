import json
import os
from datetime import UTC, datetime

import apsw
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


@pytest.mark.parametrize("bad_project", [0, False, [], 1])
def test_parse_line_rejects_non_str_project(bad_project):
    line = json.dumps({"title": "t", "body": "b", "project": bad_project})
    with pytest.raises(ValueError, match="project"):
        importer.parse_line(line)


def test_parse_line_null_project_is_omitted():
    row = importer.parse_line(json.dumps({"title": "t", "body": "b", "project": None}))
    assert row.project == ""


def test_parse_line_rejects_non_finite_timestamp():
    line = json.dumps({"title": "t", "body": "b", "created_at": 1e400})
    with pytest.raises(ValueError, match="out of range"):
        importer.parse_line(line)


def test_run_import_reports_non_finite_timestamp_as_failed_line(tmp_path, capsys):
    p = write(tmp_path, [], raw=(json.dumps({"title": "t", "body": "b", "created_at": 1e400}) + "\n").encode())
    c = cfg(tmp_path)
    assert importer.run_import(c, p, dry_run=False) == 1
    out = capsys.readouterr().out
    assert "failed 1" in out and "created 0" in out


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


def test_dry_run_matches_real_import_for_undated_row_then_stale_update(tmp_path, capsys):
    rows = [
        {"title": "t", "body": "b"},  # undated: created on both dry-run and real import "now"
        {"title": "t", "body": "old", "updated_at": 50},  # far in the past vs "now" -> skipped
    ]
    dry_path = write(tmp_path, rows)
    c_dry = cfg(tmp_path)
    assert importer.run_import(c_dry, dry_path, dry_run=True) == 0
    dry_out = capsys.readouterr().out
    assert "created 1, updated 0, skipped 1, failed 0" in dry_out

    real_path = write(tmp_path, rows)
    c_real = cfg(tmp_path)
    assert importer.run_import(c_real, real_path, dry_run=False) == 0
    real_out = capsys.readouterr().out
    assert "created 1, updated 0, skipped 1, failed 0" in real_out


def test_dry_run_simulates_duplicates_within_file(tmp_path, capsys):
    c = cfg(tmp_path)
    rows = [{"title": "t", "body": "b", "updated_at": 10}, {"title": "t", "body": "b", "updated_at": 10}]
    assert importer.run_import(c, write(tmp_path, rows), dry_run=True) == 0
    out = capsys.readouterr().out
    assert "created 1, updated 0, skipped 1, failed 0" in out
    assert not c.db_path.exists()


def test_missing_file(tmp_path):
    assert importer.run_import(cfg(tmp_path), tmp_path / "nope.jsonl", dry_run=False) == 2


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="permission bits meaningless as root / non-posix")
@pytest.mark.parametrize("dry_run", [True, False])
def test_unreadable_import_file_errors_before_touching_home(tmp_path, capsys, dry_run):
    p = tmp_path / "in.jsonl"
    p.write_text('{"title":"t","body":"b"}\n')
    p.chmod(0o000)
    try:
        c = cfg(tmp_path)
        assert importer.run_import(c, p, dry_run=dry_run) == 2
        out = capsys.readouterr().out
        assert f"error: cannot read {p}" in out
        assert not c.home.exists()
    finally:
        p.chmod(0o644)


def test_ts_rejects_negative_float():
    with pytest.raises(ValueError, match="created_at"):
        importer.parse_line(json.dumps({"title": "t", "body": "b", "created_at": -0.1}))


def test_ts_rejects_iso_just_before_epoch():
    with pytest.raises(ValueError, match="created_at"):
        importer.parse_line(json.dumps({"title": "t", "body": "b", "created_at": "1969-12-31T23:59:59.9Z"}))


def test_unopenable_db_reports_error_not_traceback(tmp_path, capsys):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        c = load_config(env={"AUSTIN_POWER_HOME": str(tmp_path / "h"), "AUSTIN_POWER_DB": str(ro / "memory.db")})
        p = write(tmp_path, [{"title": "t", "body": "b"}])
        assert importer.run_import(c, p, dry_run=False) == 1
        out = capsys.readouterr().out
        assert out.startswith("error: cannot open database ") and str(ro / "memory.db") in out
    finally:
        ro.chmod(0o700)


def test_dry_run_corrupt_db_reports_error_not_traceback(tmp_path, capsys):
    c = cfg(tmp_path)
    c.db_path.parent.mkdir(parents=True, exist_ok=True)
    c.db_path.write_bytes(os.urandom(4096))
    p = write(tmp_path, [{"title": "t", "body": "b"}])
    assert importer.run_import(c, p, dry_run=True) == 1
    out = capsys.readouterr().out
    assert out.startswith("error: cannot open database ") and str(c.db_path) in out


def test_dry_run_uninitialized_schema_reports_error_not_traceback(tmp_path, capsys):
    c = cfg(tmp_path)
    c.db_path.parent.mkdir(parents=True, exist_ok=True)
    # A valid but never-initialized (schema 0) sqlite file: no `note` table yet.
    conn = apsw.Connection(str(c.db_path))
    conn.close()
    p = write(tmp_path, [{"title": "t", "body": "b"}])
    assert importer.run_import(c, p, dry_run=True) == 1
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert out.startswith("error: cannot open database ") and str(c.db_path) in out


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
