"""Regression for v0.27.3 field-report finding #E:
KB_WRITE_AUDIT_INCLUDE_USER=1 wrote "user": "unknown" in
shells where $USER is empty but $LOGNAME is set (Claude Code,
some container environments). v0.27.4 delegates to
getpass.getuser() which walks LOGNAME / USER / LNAME / USERNAME
in order and falls back to a pwd lookup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_audit_user_uses_logname_when_user_empty(tmp_path, monkeypatch):
    """The field-report environment: empty USER, set LOGNAME.
    getpass.getuser walks USER first, then LOGNAME, so LOGNAME
    wins when USER is empty OR missing."""
    monkeypatch.setenv("KB_WRITE_AUDIT_INCLUDE_USER", "1")
    monkeypatch.setenv("LOGNAME", "llm-agent")
    # Clear USER. On some systems delenv("USER", raising=False)
    # leaves USER="", others delete it — both cases must work.
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LNAME", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)

    from kb_write.audit import record

    log_dir = tmp_path / ".kb-mcp"
    log_dir.mkdir()

    record(tmp_path, op="test", target="papers/X.md")

    lines = (log_dir / "audit.log").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["user"] == "llm-agent", (
        f"v0.27.3 regression: when USER is empty but LOGNAME is "
        f"set, opt-in audit user should resolve to LOGNAME, not "
        f"'unknown'. Got entry: {entry}"
    )


def test_audit_user_not_recorded_by_default(tmp_path):
    """Without the opt-in env var, no `user` field at all — this
    is the privacy default (v0.27.0)."""
    from kb_write.audit import record

    log_dir = tmp_path / ".kb-mcp"
    log_dir.mkdir()

    record(tmp_path, op="test", target="papers/X.md")

    lines = (log_dir / "audit.log").read_text().strip().splitlines()
    entry = json.loads(lines[0])
    assert "user" not in entry, (
        "no `user` field should be written when "
        "KB_WRITE_AUDIT_INCLUDE_USER is not set"
    )


def test_audit_user_opt_in_with_user_set(tmp_path, monkeypatch):
    """The normal case — both USER and LOGNAME set. getpass picks
    LOGNAME first per Python docs (LOGNAME → USER → LNAME →
    USERNAME). The exact order doesn't matter much to users; what
    matters is that *something* gets recorded when any of them
    is populated."""
    monkeypatch.setenv("KB_WRITE_AUDIT_INCLUDE_USER", "1")
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("LOGNAME", "alice-from-logname")

    from kb_write.audit import record

    log_dir = tmp_path / ".kb-mcp"
    log_dir.mkdir()

    record(tmp_path, op="test", target="papers/X.md")

    lines = (log_dir / "audit.log").read_text().strip().splitlines()
    entry = json.loads(lines[0])
    # Python's getpass.getuser walks LOGNAME first, then USER.
    # Documented — don't overspecify, just check it's one of them.
    assert entry["user"] in ("alice", "alice-from-logname"), (
        f"got user={entry.get('user')!r}; expected one of the two env values"
    )
    assert entry["user"] != "unknown"
