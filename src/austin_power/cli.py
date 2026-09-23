from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from austin_power import __version__
from austin_power.config import ConfigError, ensure_home, load_config, require_loopback

HOOKS_SNIPPET = {
    "hooks": {
        "PostCompact": [{"hooks": [{"type": "command", "command": "austin-power hook post-compact", "timeout": 30}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "austin-power hook session-start", "timeout": 10}]}],
    }
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="austin-power", description="Lightweight Korean-aware memory server for coding agents")
    p.add_argument("--version", action="version", version=f"austin-power {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def addr(sp):
        sp.add_argument("--host")
        sp.add_argument("--port")

    addr(sub.add_parser("serve", help="run the server in the foreground"))
    addr(sub.add_parser("status", help="check whether the server is running"))
    t = sub.add_parser("token", help="print (or rotate) the bearer token")
    t.add_argument("--rotate", action="store_true")
    s = sub.add_parser("setup", help="print registration snippets (never edits other tools' config)")
    ss = s.add_subparsers(dest="target", required=True)
    addr(ss.add_parser("claude"))
    addr(ss.add_parser("codex"))
    ss.add_parser("hooks")
    i = sub.add_parser("import", help="import memories from JSONL")
    i.add_argument("file")
    i.add_argument("--dry-run", action="store_true")
    h = sub.add_parser("hook", help="Claude Code hook entry points (read JSON on stdin)")
    h.add_argument("event", choices=["post-compact", "session-start"])
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    if args.cmd == "hook":
        from austin_power import hook

        return hook.main(args.event)

    try:
        cfg = load_config(host=getattr(args, "host", None), port=getattr(args, "port", None))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "serve":
        from austin_power.server import serve

        return serve(cfg)

    if args.cmd == "status":
        # Loopback health checks must never go through an env-configured proxy
        # (HTTP_PROXY/http_proxy etc.) — urlopen's default opener honors those
        # via ProxyHandler.from_environment(), so build a proxy-free one.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(cfg.base_url + "/health", timeout=2) as r:
                info = json.load(r)
        except Exception:  # noqa: BLE001 - any failure (refused, timeout, bad JSON) means "not running"
            print(f"not running: {cfg.base_url}")
            return 1
        if isinstance(info, dict) and info.get("status") == "ok" and info.get("name") == "austin-power":
            print(f"running: {cfg.base_url} (v{info.get('version', '?')})")
            return 0
        print(f"not running: {cfg.base_url} (port answered but it is not austin-power)")
        return 1

    from austin_power import auth

    if args.cmd == "token":
        ensure_home(cfg)
        try:
            if args.rotate:
                print(auth.rotate_token(cfg.token_path))
                print(
                    "token rotated: restart the server and re-register clients (austin-power setup claude|codex)",
                    file=sys.stderr,
                )
            else:
                print(auth.ensure_token(cfg.token_path))
        except auth.TokenError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "setup":
        if args.target == "hooks":
            print(json.dumps(HOOKS_SNIPPET, indent=2))
            return 0
        try:
            require_loopback(cfg.host)
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        ensure_home(cfg)
        try:
            token = auth.ensure_token(cfg.token_path)
        except auth.TokenError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.target == "claude":
            print(
                f'claude mcp add --transport http --scope user austin-power {cfg.mcp_url} '
                f'--header "Authorization: Bearer {token}"'
            )
            print("warning: the command above contains your token", file=sys.stderr)
        else:
            print(f"codex mcp add austin-power --url {cfg.mcp_url} --bearer-token-env-var AUSTIN_POWER_TOKEN")
            print('# add to your shell profile:\nexport AUSTIN_POWER_TOKEN="$(austin-power token)"')
        return 0

    if args.cmd == "import":
        from austin_power.importer import run_import

        return run_import(cfg, Path(args.file), dry_run=args.dry_run)

    return 2
