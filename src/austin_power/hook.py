from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from austin_power import auth
from austin_power.config import ConfigError, load_config

SUMMARY_MAX, SUMMARY_KEEP = 32000, 31000

class Unreachable(Exception): ...
class ServerError(Exception): ...

def resolve_project(cwd: str, env) -> str:
    if v := (env.get("AUSTIN_POWER_PROJECT") or "").strip():
        return v[:100]
    if not cwd:
        return ""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).name[:100]
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name[:100]

def call_tool(cfg, token: str, name: str, args: dict, timeout: float = 5.0) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(cfg.mcp_url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
    # Loopback requests must never go through an env-configured proxy (HTTP_PROXY/
    # http_proxy etc.) — build a proxy-free opener rather than urlopen's default,
    # which honors those env vars via ProxyHandler.from_environment().
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        raise ServerError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            raise Unreachable(str(e.reason)) from None
        if isinstance(e.reason, TimeoutError):
            raise TimeoutError() from None
        raise ServerError(str(e.reason)) from None
    result = payload.get("result") or {}
    if payload.get("error") or result.get("isError"):
        raise ServerError(json.dumps(payload.get("error") or result.get("content"), ensure_ascii=False)[:300])
    sc = result.get("structuredContent") or {}
    return sc["result"] if set(sc) == {"result"} and isinstance(sc["result"], dict) else sc

def _fallback_save(cfg, fields: dict, err) -> None:
    from austin_power import db, store  # heavy imports only here
    lock = db.ServerLock(cfg.lock_path)
    if not lock.acquire():
        print(f"austin-power: server running but unreachable at {cfg.mcp_url} — check AUSTIN_POWER_HOST/PORT", file=err)
        return
    try:
        conn = db.open_db(cfg.db_path, busy_timeout=20000, rebuild_allowed=True)
        try:
            store.save(conn, **fields)
        finally:
            conn.close()
    finally:
        lock.release()

def format_context(project: str, items: list[dict], cap: int) -> str:
    lines = [f"austin-power: recent memories for {project} (use the search/get tools for more)"]
    size = len(lines[0])
    for it in items:
        line = f"- [{it['kind']}] {it['title']} (#{it['id']}, {it['updated_at'][:10]}): {it['preview']}".replace("\n", " ")
        if size + 1 + len(line) > cap:
            break
        lines.append(line); size += 1 + len(line)
    return "\n".join(lines) if len(lines) > 1 else ""

def _post_compact(data: dict, cfg, env, out, err) -> None:
    summary = data.get("compact_summary")
    sid = data.get("session_id")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(sid, str) or not sid:
        return
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_KEEP] + "\n…(truncated)"
    fields = {"title": "session " + sid[:180], "body": summary, "kind": "session",
              "project": resolve_project(data.get("cwd") or "", env), "session_id": sid[:200]}
    try:
        token = auth.read_token(cfg.token_path)
    except auth.TokenError:
        _fallback_save(cfg, fields, err); return
    try:
        call_tool(cfg, token, "save", fields)
    except Unreachable:
        _fallback_save(cfg, fields, err)
    except TimeoutError:
        print("austin-power: server timed out; summary not saved", file=err)
    except ServerError as e:
        print(f"austin-power: save failed: {e}", file=err)

def _session_start(data: dict, cfg, env, out, err) -> None:
    if data.get("source") == "compact":
        return
    project = resolve_project(data.get("cwd") or "", env)
    if not project:
        return
    try:
        token = auth.read_token(cfg.token_path)
        res = call_tool(cfg, token, "recent", {"project": project, "limit": 8})
    except (auth.TokenError, Unreachable, ServerError, TimeoutError):
        return
    text = format_context(project, res.get("results", []), cfg.inject_chars)
    if text:
        out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))

def main(event: str, *, stdin=None, stdout=None, stderr=None, env=None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    env = os.environ if env is None else env
    try:
        data = json.loads(stdin.read() or "null")
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")  # noqa: TRY004 - fail-open input validation, not a type-mismatch bug
        if not isinstance(data.get("cwd", ""), str):
            raise ValueError("cwd must be a string")  # noqa: TRY004
        cfg = load_config(env=env)
        (_post_compact if event == "post-compact" else _session_start)(data, cfg, env, stdout, stderr)
    except (ValueError, ConfigError) as e:
        print(f"austin-power hook: ignored invalid input: {e}", file=stderr)
    except Exception as e:  # noqa: BLE001 - fail-open: never break the user's session
        print(f"austin-power hook: {type(e).__name__}: {e}", file=stderr)
    return 0
