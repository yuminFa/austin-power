import apsw
import pytest

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
    c = db.open_db(tmp_path / "m.db")
    c.execute("pragma user_version=2")
    c.close()
    with pytest.raises(db.SchemaTooNewError):
        db.open_db(tmp_path / "m.db")


def test_signature_mismatch_rebuilds_when_allowed(tmp_path, caplog):
    c = db.open_db(tmp_path / "m.db")
    c.execute("insert into note(project,kind,title,body,created_at,updated_at) values('','fact','t','인덱스를',1,1)")
    c.execute("update meta set value='old' where key='tokenizer_sig'")
    c.close()
    c = db.open_db(tmp_path / "m.db", rebuild_allowed=True)
    assert c.execute("select value from meta where key='tokenizer_sig'").fetchone()[0] == tokenizer.signature()
    assert c.execute("select rowid from note_fts where note_fts match '\"인덱스\"'").fetchall() == [(1,)]


def test_signature_mismatch_refuses_when_not_allowed(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    c.execute("update meta set value='old' where key='tokenizer_sig'")
    c.close()
    with pytest.raises(db.TokenizerMismatchError):
        db.open_db(tmp_path / "m.db", rebuild_allowed=False)


def test_signature_mismatch_refuses_write(tmp_path):
    c = db.open_db(tmp_path / "m.db")
    c.execute("update meta set value='other' where key='tokenizer_sig'")
    with pytest.raises(db.TokenizerMismatchError), db.write_txn(c):
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
