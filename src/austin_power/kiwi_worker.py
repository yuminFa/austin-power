"""Kiwi worker child process (spec §2.5.1).

`python -m austin_power.kiwi_worker` loads `Kiwi()` (same defaults as
`tokenizer.get_kiwi`), prints one JSON line `{"ready": true, "signature": ...}`,
then loops reading JSON lines `{"text": ...}` from stdin and writing
`{"tokens": [[form, tag, start, len], ...]}` or `{"error": ...}` — one line
per request, UTF-8, newline-delimited. Exits 0 on stdin EOF.

Must not import `mcp`/`uvicorn` (kept out of the hot worker-startup path;
also asserted by tests/test_kiwi_worker.py).
"""

from __future__ import annotations

import json
import os
import sys

from austin_power import tokenizer


def main() -> int:
    # Don't gamble on the parent's locale env (test/service supervisors may
    # start us with a minimal env) — the wire is meant to be plain ASCII
    # anyway since both sides use ensure_ascii=True, but make the stdio
    # encoding explicit regardless.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    kiwi = tokenizer.get_kiwi()
    sig = os.environ.get("AUSTIN_POWER__TEST_FAKE_SIG") or tokenizer.signature()
    sys.stdout.write(json.dumps({"ready": True, "signature": sig}, ensure_ascii=True) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            toks = [[t.form, t.tag, t.start, t.len] for t in kiwi.tokenize(req["text"])]
            resp = {"tokens": toks}
        except Exception as e:  # noqa: BLE001 - report to parent, never crash the worker
            resp = {"error": f"{type(e).__name__}: {e}"}
        try:
            sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
