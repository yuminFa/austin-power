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
- PostCompact and SessionEnd also spawn a background worker that sends session text to an external LLM CLI (`codex exec`, falling back to `claude -p` if codex is absent or fails) to distill up to 8 reusable memories. This is separate from the server, which still never calls an LLM. 0-2 LLM calls per session end/compact (0 if the input is under 200 chars or extraction is off, usually 1, 2 if codex fails and claude is tried — this can cost money). Disable with `AUSTIN_POWER_EXTRACT=off`; logs go to `<home>/extract.log` (never the prompt or memory bodies). Existing memories are checked first to avoid duplicates — see "Automatic extraction and dedup" below.

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
austin-power setup hooks    # -> Claude Code PostCompact/SessionStart/SessionEnd hook JSON
```

See the Korean README's "Claude Code / Codex에 등록하기" section for real, byte-for-byte output.

### Codex CLI hooks

Codex CLI supports its own hook events (`PreCompact`, `SessionEnd`, etc.), separate from Claude Code's. Register these in `~/.codex/hooks.json` (outside this repo, your local config):

```json
{
  "hooks": {
    "PreCompact": [
      {"matcher": "manual|auto", "hooks": [{"type": "command", "command": "austin-power hook pre-compact", "timeout": 30}]}
    ],
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "austin-power hook session-end", "timeout": 10}]}
    ]
  }
}
```

`pre-compact` is never registered with Claude Code (Claude uses `PostCompact`'s own summary instead) — it's Codex-only, and hands the worker the Codex rollout transcript just before compaction so it can be parsed per §2.12. Both the current Codex rollout shape (`{"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage"|"AgentMessage",...}}}`, with `{"type":"compacted"}` as the compaction boundary) and the legacy shape (`payload.type` of `user_message`/`agent_message`) are auto-detected.

### Automatic extraction and dedup

So that repeated compactions don't pile up the same facts, the worker looks at existing memories before extracting.

1. It reads up to 50 recent memories of the same project (excluding `kind=session`) from the local DB, read-only, and puts them in the prompt.
2. The LLM picks an `action` per item:
   - already known → not returned (skip)
   - refines an existing memory → `update`: the exact existing title, with a body merging old and new content
   - new fact → `new`: a new title
3. The worker re-checks the result:
   - an `update` whose title doesn't match an existing one is saved as `new`;
   - a `new` whose title matches an existing one is treated as `update` (same title means overwrite);
   - if the merged body is shorter than 70% of the old body, it is dropped and the old body stays (shrink guard);
   - memories shown title-only (body too long) or whose titles differ only by whitespace are not update targets;
   - an update always keeps the existing `kind`;
   - memories containing credential-like values (GitHub/AWS/Slack tokens, PEM private keys, etc.) never have their body sent to the LLM (the whole row is omitted if the title contains one) and are not update targets.
4. Extractions for the same project are serialized with a per-project file lock, so two workers can't overwrite each other's merge.

Note: because of step 1, existing memory bodies of the same project are sent to the extraction CLI's provider, just like session text. Secret detection only catches common patterns and is a mitigation, so don't store secrets in memories.

Each run writes one line to `<home>/extract.log`:

```
2026-09-23T14:26:08Z source=compact session=1a2b3c4d backend=codex saved=2 failed=0 updated=1 dropped=0
```

| Field | Meaning |
|---|---|
| `saved` | memories written (new + updated) |
| `updated` | of those, existing memories updated in place |
| `dropped` | discarded (e.g. by the shrink guard) |
| `skipped=short\|off\|busy\|badjob` | input too short / extraction off / timed out waiting for the project lock / malformed job file |

Limits: older memories outside the recent 50 and similar memories with different titles are left to the LLM's judgment, so some duplicates can remain. Existing duplicates are not cleaned up automatically (remove them with `forget`).

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
| `AUSTIN_POWER_EXTRACT` | `auto` | memory-extraction mode: `auto` (codex→claude fallback), `codex`, `claude`, `off` |
| `AUSTIN_POWER_CODEX_BIN` | `codex` | primary extraction backend executable |
| `AUSTIN_POWER_CODEX_MODEL` | (unset) | falls back to codex's own default model when unset |
| `AUSTIN_POWER_CLAUDE_BIN` | `claude` | fallback extraction backend executable |
| `AUSTIN_POWER_CLAUDE_MODEL` | `sonnet` | |
| `AUSTIN_POWER_EXTRACT_TIMEOUT` | `180` | per-backend call timeout, seconds (10-1800) |

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
- Windows untested (the extraction worker uses POSIX `start_new_session`/`killpg`)

## License

MIT. Full design: [`docs/design.md`](docs/design.md).
