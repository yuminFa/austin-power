# contrib

`agentmemory_to_jsonl.py` converts an agentmemory KV
store (`mem%3Amemories.bin` / `mem%3Asessions.bin` / `mem%3Asummaries.bin`)
into austin-power's JSONL import format, including session summaries as
`kind="session"` rows (pass `--no-summaries` to skip them). Insights and
semantic auto-derived facts are intentionally not converted — they're
agentmemory-internal derived data, not source-of-truth notes.

Usage: `python contrib/agentmemory_to_jsonl.py [STORE_DIR] [-o OUT.jsonl]`,
then `austin-power import OUT.jsonl`.

This script is not part of the core package; its record format was
reverse-engineered from a local store and may break with future
agentmemory versions.
