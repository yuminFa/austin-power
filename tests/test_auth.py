import threading

import pytest

from austin_power.auth import TokenError, check_bearer, ensure_token, read_token, rotate_token


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
