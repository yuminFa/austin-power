#!/usr/bin/env python3
"""Convert an agentmemory KV store into austin-power JSONL (stdlib only).

usage: python agentmemory_to_jsonl.py [STORE_DIR] [-o OUT.jsonl]
then:  austin-power import OUT.jsonl
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

TITLE_MAX, BODY_MAX, BODY_KEEP = 200, 32000, 31000

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.JSONDecoder().raw_decode(raw)[0]  # tolerate trailing garbage after the object

def _kind(t) -> str:
    k = re.sub(r"[^a-z0-9_-]", "-", str(t or "").lower())
    k = re.sub(r"-{2,}", "-", k).strip("-_")[:32]
    return k or "fact"

def _frontmatter(text: str) -> dict:
    m = re.match(r"^\s*(?:\[[^\]\n]*\]\s*)?---\s*\n(.*?)\n---\s*(?:\n|$)", text or "", re.DOTALL)
    out: dict = {}
    if m:
        for line in m.group(1).splitlines():
            key, sep, val = line.strip().partition(":")
            if sep and key.strip() and key.strip() not in out and val.strip():
                out[key.strip()] = val.strip().strip("'\"")
    return out

def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and line != "---" and not re.fullmatch(r"\[[^\]]*\]\s*---", line):
            return line
    return ""

def convert(store_dir: Path) -> tuple[list[dict], list[str]]:
    mems = _load(Path(store_dir) / "mem%3Amemories.bin")
    sessions = _load(Path(store_dir) / "mem%3Asessions.bin")
    rows, skips, seen = [], [], Counter()
    for mid in sorted(mems):
        m = mems[mid]
        try:
            if not isinstance(m, dict) or m.get("isLatest") is False:
                continue
            content = m["content"]
            fm = _frontmatter(content) or _frontmatter(m.get("title", ""))
            projects = Counter(sessions.get(s, {}).get("project") for s in m.get("sessionIds") or [])
            projects.pop(None, None); projects.pop("", None)
            project = (min(projects.items(), key=lambda kv: (-kv[1], kv[0]))[0] if projects else fm.get("project", ""))[:100]
            title = (fm.get("title") or _first_line(m.get("title", "")) or _first_line(content)).strip()
            if not title or not content.strip():
                raise ValueError("empty title or content")
            base = title[:TITLE_MAX]
            seen[(project, base)] += 1
            n = seen[(project, base)]
            if n > 1:
                suffix = f" ({n})"
                base = title[:TITLE_MAX - len(suffix)] + suffix
            body = content
            if m.get("concepts"):
                body += "\n\nconcepts: " + ", ".join(map(str, m["concepts"]))
            if m.get("files"):
                body += ("\n" if m.get("concepts") else "\n\n") + "files: " + ", ".join(map(str, m["files"]))
            if len(body) > BODY_MAX:
                body = body[:BODY_KEEP] + "\n…(truncated)"
            rows.append({"title": base, "body": body, "project": project, "kind": _kind(m.get("type")),
                         "created_at": m.get("createdAt"), "updated_at": m.get("updatedAt")})
        except (KeyError, TypeError, ValueError) as e:
            skips.append(f"skip {mid}: {type(e).__name__}: {e}")
    return rows, skips

@contextlib.contextmanager
def _output(path: str | None):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            yield f
    else:
        yield sys.stdout

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("store", nargs="?", default=str(Path.home() / ".agentmemory" / "data" / "state_store.db"))
    p.add_argument("-o", "--output")
    a = p.parse_args(argv)
    rows, skips = convert(Path(a.store).expanduser())
    with _output(a.output) as out:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    for s in skips:
        print(s, file=sys.stderr)
    print(f"converted {len(rows)}, skipped {len(skips)}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
