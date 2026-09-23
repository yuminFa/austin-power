"""Docs stay honest about the real CLI/tool/config surface.

These tests don't exercise austin_power's runtime behavior (that's the rest
of the suite) — they exercise README.md / README.en.md / docs/design.md /
examples/ / .github/workflows/ci.yml, so if code and docs drift apart a test
fails instead of a stale doc shipping quietly. Task 8 (docs, examples, CI).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

from austin_power import config
from austin_power.cli import HOOKS_SNIPPET, main

ROOT = Path(__file__).resolve().parents[1]
README_KO = ROOT / "README.md"
README_EN = ROOT / "README.en.md"
DESIGN = ROOT / "docs" / "design.md"
PLIST = ROOT / "examples" / "launchd" / "io.github.yuminfa.austin-power.plist"
SYSTEMD = ROOT / "examples" / "systemd" / "austin-power.service"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

TOOL_NAMES = ["save", "search", "get", "recent", "forget"]
ENV_VARS = [
    "AUSTIN_POWER_HOME",
    "AUSTIN_POWER_DB",
    "AUSTIN_POWER_HOST",
    "AUSTIN_POWER_PORT",
    "AUSTIN_POWER_INJECT_CHARS",
    "AUSTIN_POWER_LOG_LEVEL",
    "AUSTIN_POWER_PROJECT",
    "AUSTIN_POWER_TOKEN",
]

# Headings the Korean README must carry, in this order (spec Task 8 Step 1).
SECTION_HEADINGS_KO = [
    "## 왜 만들었나",
    "## 특징",
    "## 한국어 검색 예시",
    "## 설치·실행",
    "## Claude Code / Codex에 등록하기",
    "## 도구 5개",
    "## 설정",
    "## 데이터 위치와 백업",
    "## agentmemory에서 옮겨 오기",
    "## 한계 (v0.1)",
    "## 라이선스",
]


def _text(path: Path) -> str:
    if not path.is_file():
        pytest.fail(f"missing doc file: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def _run_cli(args, tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTIN_POWER_HOME", str(tmp_path / "h"))
    for var in ("AUSTIN_POWER_PORT", "AUSTIN_POWER_HOST", "AUSTIN_POWER_DB"):
        monkeypatch.delenv(var, raising=False)
    code = main(args)
    return code


# --- README.md (Korean) ------------------------------------------------


def test_readme_ko_sections_in_order():
    text = _text(README_KO)
    assert text.startswith("# austin-power")
    positions = [text.find(h) for h in SECTION_HEADINGS_KO]
    for heading, pos in zip(SECTION_HEADINGS_KO, positions):
        assert pos != -1, f"missing heading: {heading}"
    assert positions == sorted(positions), "sections are out of order"


def test_readme_ko_tagline():
    text = _text(README_KO)
    assert "코딩 에이전트를 위한 가볍고 한국어에 강한 기억 저장소" in text


def test_readme_mentions_agentmemory_only_in_migration_section():
    """spec: don't disparage a specific project by name outside the migration section."""
    text = _text(README_KO)
    migration_pos = text.find("## agentmemory에서 옮겨 오기")
    assert migration_pos != -1
    first_mention = text.lower().find("agentmemory")
    assert first_mention == migration_pos + len("## ".lower()) or first_mention >= migration_pos, (
        "agentmemory named before its own migration section"
    )
    # no other heading may name it
    for m in re.finditer("agentmemory", text, re.IGNORECASE):
        assert m.start() >= migration_pos, "agentmemory mentioned outside the migration section"


def test_readme_ko_tools_table_matches_server():
    text = _text(README_KO)
    src = (ROOT / "src" / "austin_power" / "server.py").read_text(encoding="utf-8")
    real_tools = re.findall(r"@srv\.tool\(.*?\n\s*async def (\w+)\(", src, re.DOTALL)
    assert real_tools == TOOL_NAMES, "TOOL_NAMES fixture drifted from server.py"
    for name in TOOL_NAMES:
        assert f"`{name}`" in text, f"tool {name!r} missing from README"


def test_readme_ko_config_table_matches_config_module():
    text = _text(README_KO)
    for var in ENV_VARS:
        assert var in text, f"{var} missing from README config table"
    assert config.DEFAULT_HOST in text
    assert str(config.DEFAULT_PORT) in text
    assert str(config.DEFAULT_INJECT_CHARS) in text
    assert "memory.db" in text
    assert "server.lock" in text


def test_readme_ko_install_and_repo_url():
    text = _text(README_KO)
    assert "uv tool install git+https://github.com/yuminFa/austin-power" in text
    assert "austin-power serve" in text


def test_readme_ko_backup_guidance():
    text = _text(README_KO)
    assert 'sqlite3 memory.db ".backup backup.db"' in text
    assert "WAL" in text


def test_readme_ko_migration_command():
    text = _text(README_KO)
    assert "python contrib/agentmemory_to_jsonl.py" in text
    assert "austin-power import" in text


def test_readme_ko_license_and_design_link():
    text = _text(README_KO)
    assert "MIT" in text
    assert "docs/design.md" in text


@pytest.mark.parametrize("target,port", [("claude", None), ("codex", None)])
def test_readme_ko_setup_output_matches_real_cli(target, port, tmp_path, monkeypatch, capsys):
    args = ["setup", target] + (["--port", port] if port else [])
    code = _run_cli(args, tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    if target == "claude":
        m = re.search(r"Bearer ([A-Za-z0-9_-]{40,})", out)
        assert m, f"no token found in setup claude output: {out!r}"
        redacted = out.replace(m.group(1), "<TOKEN>")
        text = _text(README_KO)
        assert redacted.strip() in text, "README claude setup example does not match real CLI output"
    else:
        text = _text(README_KO)
        assert out.strip() in text, "README codex setup example does not match real CLI output"


def test_readme_ko_setup_hooks_matches_real_cli(tmp_path, monkeypatch, capsys):
    code = _run_cli(["setup", "hooks"], tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["hooks"] == HOOKS_SNIPPET["hooks"]
    text = _text(README_KO)
    assert out.strip() in text, "README hooks JSON does not match real `austin-power setup hooks` output"


@pytest.mark.kiwi
def test_readme_ko_korean_search_example_is_true():
    """Every (저장, 검색어) pair in the README's Korean-search table must really match
    through the real kiwi FTS5 tokenizer — not just be printed."""
    import apsw

    from austin_power.tokenizer import register

    text = _text(README_KO)
    section = text[text.find("## 한국어 검색 예시") : text.find("## 설치·실행")]
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", section, re.MULTILINE)
    assert len(rows) >= 2, "expected at least 2 example rows in the Korean-search table"

    conn = apsw.Connection(":memory:")
    register(conn)
    conn.execute("create virtual table f using fts5(b, tokenize='kiwi')")
    body_to_id = {b: i for i, b in enumerate(dict.fromkeys(r[0] for r in rows), start=1)}
    for body, i in body_to_id.items():
        conn.execute("insert into f(rowid, b) values (?, ?)", (i, body))

    for body, query in rows:
        hit = conn.execute(
            "select 1 from f where rowid=? and f match ?", (body_to_id[body], f'"{query}"')
        ).fetchone()
        assert hit, f"query {query!r} does not match saved text {body!r}"


# --- README.en.md --------------------------------------------------------


def test_readme_en_exists_and_covers_the_essentials():
    text = _text(README_EN)
    assert text.startswith("# austin-power")
    for name in TOOL_NAMES:
        assert f"`{name}`" in text
    assert "uv tool install git+https://github.com/yuminFa/austin-power" in text
    assert "docs/design.md" in text
    assert "MIT" in text


# --- docs/design.md --------------------------------------------------------


def test_design_doc_has_diagram_and_principles():
    text = _text(DESIGN)
    assert "```mermaid" in text
    for phrase in ["단일", "SQLite", "LLM", "무상태", "HTTP"]:
        assert phrase in text


def test_design_doc_has_deviation_table():
    text = _text(DESIGN)
    assert "원래 설계" in text or "원 설계" in text
    assert "v0.1" in text


# --- examples ----------------------------------------------------------


def test_launchd_plist_is_well_formed():
    raw = PLIST.read_bytes() if PLIST.is_file() else pytest.fail(f"missing {PLIST}")
    d = plistlib.loads(raw)
    assert d["Label"] == "io.github.yuminfa.austin-power"
    assert d["ProgramArguments"][-1] == "serve"
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] is True
    assert d["StandardErrorPath"] == "/tmp/austin-power.log"
    text = raw.decode("utf-8")
    assert "which austin-power" in text


def test_systemd_unit_has_required_directives():
    text = _text(SYSTEMD)
    assert "ExecStart=%h/.local/bin/austin-power serve" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert "systemctl --user enable --now austin-power" in text


# --- CI ------------------------------------------------------------------


def test_ci_workflow_covers_the_matrix_and_steps():
    text = _text(CI_WORKFLOW)
    required_in_order = [
        "ubuntu-latest",
        "macos-latest",
        '"3.11"',
        '"3.13"',
        "actions/checkout",
        "astral-sh/setup-uv@v6",
        "uv sync --python",
        "uv run ruff check",
        "uv run pytest -q",
    ]
    positions = [text.find(s) for s in required_in_order]
    for needle, pos in zip(required_in_order, positions):
        assert pos != -1, f"CI workflow missing: {needle}"
    # steps (last 5 entries) must appear in pipeline order
    assert positions[4:] == sorted(positions[4:])


# --- portability (Task 8 Step 6) -----------------------------------------


def test_portability_no_personal_identifiers():
    denylist_path = os.environ.get("AUSTIN_POWER_DENYLIST")
    if not denylist_path:
        pytest.skip(
            "AUSTIN_POWER_DENYLIST not set — the deny-list is local and uncommitted; "
            "run with it set to actually check portability"
        )
    patterns = [
        line.strip()
        for line in Path(denylist_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    hits = []
    for rel in tracked:
        p = ROOT / rel
        try:
            content = p.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for pat in patterns:
            if pat.lower() in content:
                hits.append(f"{rel}: {pat!r}")
    assert not hits, "personal identifiers found in tracked files: " + ", ".join(hits)
