import pytest

from austin_power import db, store
from austin_power.store import ImportRow, ValidationError

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
    assert conn.execute("select kind, session_id, created_at, updated_at from note").fetchone() == (
        "decision",
        "s1",
        1,
        2,
    )


def test_project_scopes_uniqueness(conn):
    store.save(conn, title="t", body="b", project="a")
    assert store.save(conn, title="t", body="b", project="b")[1] == "created"


@pytest.mark.parametrize(
    "kw,field",
    [
        ({"title": "", "body": "b"}, "title"),
        ({"title": "x" * 201, "body": "b"}, "title"),
        ({"title": "t", "body": ""}, "body"),
        ({"title": "t", "body": "x" * 32001}, "body"),
        ({"title": "t", "body": "b", "kind": "bad kind"}, "kind"),
        ({"title": "t", "body": "b", "project": "p" * 101}, "project"),
        ({"title": "t", "body": "b", "session_id": "s" * 201}, "session_id"),
    ],
)
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


@pytest.mark.parametrize("q", ['"', "***", "NEAR(a b)", "-x", "title:foo", "a AND OR", "^", "을 를"])
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
    r = lambda **k: ImportRow(
        **{
            "title": "t",
            "body": "b",
            "project": "",
            "kind": None,
            "session_id": None,
            "created_at": None,
            "updated_at": None,
            **k,
        }
    )
    with db.write_txn(conn):
        assert store.import_row(conn, r(created_at=10, updated_at=20)) == "created"
        assert store.import_row(conn, r(created_at=10, updated_at=20)) == "skipped"  # identical
        assert store.import_row(conn, r(updated_at=15, created_at=10)) == "skipped"  # older
        assert store.import_row(conn, r()) == "skipped"  # no timestamps, exists
        assert store.import_row(conn, r(body="new", created_at=5, updated_at=30)) == "updated"
    assert conn.execute("select body, created_at, updated_at from note").fetchone() == ("new", 10, 30)


def test_import_explicit_empty_session_id_is_a_real_change(conn):
    store.save(conn, title="t", body="b", session_id="s1", now=10)
    row = ImportRow(title="t", body="b", project="", kind=None, session_id="", created_at=10, updated_at=10)
    with db.write_txn(conn):
        assert store.import_row(conn, row) == "updated"
    assert conn.execute("select session_id from note").fetchone() == ("",)
