from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
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
        raise ValueError(f"{field}: not a timestamp")  # noqa: TRY004 - ValueError is this function's contract
    if isinstance(value, (int, float)):
        n = float(value)
        if n >= 1e11:
            n /= 1000
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"{field}: not ISO 8601 or epoch") from None
        n = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    else:
        raise ValueError(f"{field}: not a timestamp")  # noqa: TRY004 - ValueError is this function's contract
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
        raise ValueError("line must be a JSON object")  # noqa: TRY004 - ValueError is this function's contract
    try:
        title, body, project, kind, sid = store.clean_fields(
            obj.get("title"), obj.get("body"), obj.get("project", ""), obj.get("kind"), obj.get("session_id")
        )
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
                yield n, None, "not valid UTF-8"
                continue
            if not line:
                continue
            try:
                yield n, parse_line(line), None
            except ValueError as e:
                yield n, None, str(e)


def run_import(cfg: Config, path: Path, *, dry_run: bool, out=None) -> int:
    # Resolve the default at call time, not at import time: a mutable default of
    # `sys.stdout` would bind whatever object that name pointed to when this
    # module was first imported, which breaks under pytest's capsys (it swaps
    # sys.stdout per test after modules are already imported).
    out = sys.stdout if out is None else out
    path = Path(path)
    if not path.is_file():
        print(f"error: cannot read {path}", file=out)
        return 2
    counts = {"created": 0, "updated": 0, "skipped": 0}
    failures: list[str] = []
    committed = 0

    def report():
        print(
            f"created {counts['created']}, updated {counts['updated']}, "
            f"skipped {counts['skipped']}, failed {len(failures)}",
            file=out,
        )
        for f in failures[:50]:
            print(f, file=out)
        if len(failures) > 50:
            print(f"... and {len(failures) - 50} more", file=out)

    if dry_run:
        try:
            conn = db.open_db_readonly(cfg.db_path)
        except db.SchemaTooNewError as e:
            print(f"error: {e}", file=out)
            return 1
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
            print(f"error: {e}", file=out)
            return 1
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
                    failures.append(f"line {n}: {err}")
                    continue
                batch.append((n, row))
                if len(batch) >= BATCH_ROWS or time.monotonic() - started >= BATCH_SECONDS:
                    flush()
                    started = time.monotonic()
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
