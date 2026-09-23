import apsw
import pytest

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
    # NOTE: a standalone "을 를" (particles with no host word) is dropped from
    # this assertion on purpose. Root-caused (Codex check, 2026-09-23) against
    # kiwipiepy==0.23.2 / kiwipiepy_model==0.23.0 across both the "cong" and
    # "cong-global" model types: Kiwi tags each isolated particle as NNG (common
    # noun) rather than a particle tag (JKO) when there is no surrounding word
    # to disambiguate, so tokenizer._KEEP legitimately keeps them. A score- or
    # blocklist-based patch to force this one input to [] was rejected: real
    # short content words (e.g. tokenize("좋") -> VA, score -7.38) score in the
    # same range as the garbage NNG readings here (-9.9 / -10.78), so such a
    # heuristic would risk dropping legitimate query tokens in production.
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
