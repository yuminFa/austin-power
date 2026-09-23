# austin-power

Lightweight, Korean-aware memory server for coding agents (MCP over local HTTP).

[한국어](README.md) · [design doc](docs/design.md) · MIT License

## Why

Built after real-world friction with a general-purpose memory server: heavy resident memory for a small dataset (~550MB for the same 1MB dataset), several process layers per session, an index that keeps accumulating full-rebuild generations, and Korean search that misses because particles (조사) are indexed as part of the word. austin-power fixes this with an incremental SQLite FTS5 index, a single resident process (plus a Kiwi worker child that self-terminates when idle), and a Korean morphological tokenizer (Kiwi).

### Memory: idle vs. active

Kiwi's model (~400MB) doesn't live in the server process — it's loaded only in a separate `kiwi_worker` child. The worker starts on the first call that needs Korean analysis (save/search) and exits on its own after `AUSTIN_POWER_KIWI_IDLE` seconds without requests. So there are two numbers:

- **Idle** (server process only — right after boot, or after the worker idled out) — about **69.5MB**.
- **Active** (server + worker combined, peak observed over 1,000 `save`/`search` calls) — about **580MB**.
- The worker self-terminates after `AUSTIN_POWER_KIWI_IDLE` seconds (default 600, see [Configuration](#configuration)) of no requests, returning that ~400MB to the OS. The next call after that respawns and re-handshakes the worker, adding about **0.87s** of latency.
- If the tokenizer rules ever change, forcing a full reindex, restarting the server with 1,000 existing notes — worker respawn, model reload and the FTS5 rebuild all included — takes about **1.22s**.
- Measured 2026-09-23 (macOS arm64, `scripts/measure_rss.py`) against spec §2.5.1's targets (server alone ≤120MB · active total ≤650MB · first call after idle unload ≤3s · rebuild 1,000 notes ≤15s) — all four passed.

## Features

- One SQLite file — the server, CLI and hooks all read/write the same `memory.db`.
- Incremental FTS5 index — triggers update only the changed row, never a full rebuild.
- Kiwi morphological analysis + identifier preservation — Korean particles/endings are stripped, but code identifiers like `note_fts` still match on their exact form.
- The server never calls an LLM — search/save are plain SQL; summaries are produced by the client (Claude Code) and merely stored.
- PostCompact auto-save — the compaction summary Claude Code already produced is saved as a session memory, with zero extra LLM calls.

## Install & run

```sh
uv tool install git+https://github.com/yuminFa/austin-power
austin-power serve
```

`austin-power serve` runs in the foreground. To keep it resident, see the service examples: [launchd (macOS)](examples/launchd/io.github.yuminfa.austin-power.plist) · [systemd --user (Linux)](examples/systemd/austin-power.service).

## Register with Claude Code / Codex

`austin-power setup <target>` only **prints** a snippet — it never edits another tool's config file.

```sh
austin-power setup claude   # -> claude mcp add ...
austin-power setup codex    # -> codex mcp add ...
austin-power setup hooks    # -> Claude Code PostCompact/SessionStart hook JSON
```

See the Korean README's "Claude Code / Codex에 등록하기" section for real, byte-for-byte output.

## Tools

| Tool | What it does |
|---|---|
| `save` | Save a memory; same `(project, title)` upserts |
| `search` | Korean-aware full-text search; returns excerpts |
| `get` | Fetch one memory's full body by id |
| `recent` | Most recently updated memories, optionally scoped to a project |
| `forget` | Delete a memory by id |

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AUSTIN_POWER_HOME` | `~/.config/austin-power` | data home directory |
| `AUSTIN_POWER_DB` | `<home>/memory.db` | SQLite file path |
| `AUSTIN_POWER_HOST` | `127.0.0.1` | bind host (v0.1 is loopback-only) |
| `AUSTIN_POWER_PORT` | `7760` | server port |
| `AUSTIN_POWER_INJECT_CHARS` | `4000` | SessionStart injection cap (characters) |
| `AUSTIN_POWER_LOG_LEVEL` | `INFO` | server log level |
| `AUSTIN_POWER_KIWI_IDLE` | `600` | seconds of no requests before the Kiwi worker process unloads. `0` disables idle unload |
| `AUSTIN_POWER_PROJECT` | (unset) | overrides hook project auto-detection |
| `AUSTIN_POWER_TOKEN` | (unset) | read by Codex only, not by the server itself |

## Data & backup

All data lives under `$AUSTIN_POWER_HOME/memory.db`. Back it up with `sqlite3 memory.db ".backup backup.db"` — copying the file directly can lose recent writes because it's WAL-mode. Read the file with any sqlite3 client if you like, but never write to it outside austin-power (that skips the FTS5 tokenizer registration and desyncs the index). Session summaries may contain secrets — use `forget` to remove one.

## Migrating from agentmemory

```sh
python contrib/agentmemory_to_jsonl.py -o am.jsonl
austin-power import am.jsonl
```

Session compaction summaries (`mem%3Asummaries.bin`) are converted too (disable with `--no-summaries`). Details: [`contrib/README.md`](contrib/README.md).

## Limitations (v0.1)

- Local only (loopback bind, no remote access)
- No SessionEnd distillation (the server never calls an LLM, so it has no way to produce a summary)
- Windows untested

## License

MIT. Full design: [`docs/design.md`](docs/design.md).
