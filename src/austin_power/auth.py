from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("austin_power.auth")


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
    if os.name == "posix":
        mode = path.stat().st_mode
        if mode & 0o077:
            log.warning(
                "token file %s is readable by group/other (mode %04o); run `chmod 600 %s`",
                path, mode & 0o777, path,
            )
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
