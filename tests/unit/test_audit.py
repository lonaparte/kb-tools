"""Tests for kb_write.audit — the v27 host-identity opt-out.

Default behaviour: audit.log contains NO pid and NO user.
Opt-in via env vars."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kb_write.audit import record, AUDIT_REL_PATH


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / ".kb-mcp").mkdir()
    return tmp_path


def _last_entry(kb: Path) -> dict:
    """Read the most recent audit entry."""
    text = (kb / AUDIT_REL_PATH).read_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _clear_audit_env(monkeypatch):
    monkeypatch.delenv("KB_WRITE_AUDIT_INCLUDE_PID", raising=False)
    monkeypatch.delenv("KB_WRITE_AUDIT_INCLUDE_USER", raising=False)


class TestDefaultNoHostIdentity:
    def test_no_pid_by_default(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        record(kb, op="test", target="papers/X", actor="cli")
        entry = _last_entry(kb)
        assert "pid" not in entry, (
            "regression: audit.log leaked pid by default — v27 opted "
            "this out to prevent host-identity leakage through "
            "shared snapshots"
        )

    def test_no_user_by_default(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        record(kb, op="test", target="papers/X", actor="cli")
        entry = _last_entry(kb)
        assert "user" not in entry, (
            "regression: audit.log leaked user by default — see "
            "CHANGELOG v27 security section"
        )

    def test_core_fields_still_present(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        record(kb, op="test_op", target="papers/X", actor="cli")
        entry = _last_entry(kb)
        # These must still be recorded — they're the whole point.
        assert entry["op"] == "test_op"
        assert entry["target"] == "papers/X"
        assert entry["actor"] == "cli"
        assert "ts" in entry


class TestOptIn:
    def test_pid_opt_in(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        monkeypatch.setenv("KB_WRITE_AUDIT_INCLUDE_PID", "1")
        record(kb, op="x", target="papers/X", actor="cli")
        entry = _last_entry(kb)
        assert entry.get("pid") == os.getpid()

    def test_user_opt_in(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        monkeypatch.setenv("KB_WRITE_AUDIT_INCLUDE_USER", "1")
        record(kb, op="x", target="papers/X", actor="cli")
        entry = _last_entry(kb)
        assert "user" in entry
        assert isinstance(entry["user"], str)

    def test_opt_in_accepts_multiple_truthy(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        for val in ("1", "true", "yes", "on", "TRUE", "YES"):
            monkeypatch.setenv("KB_WRITE_AUDIT_INCLUDE_PID", val)
            record(kb, op="x", target=f"papers/{val}", actor="cli")
            entry = _last_entry(kb)
            assert "pid" in entry, f"truthy value {val!r} not accepted"

    def test_zero_is_falsy(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        monkeypatch.setenv("KB_WRITE_AUDIT_INCLUDE_PID", "0")
        record(kb, op="x", target="papers/X", actor="cli")
        entry = _last_entry(kb)
        assert "pid" not in entry


class TestRobustness:
    def test_never_raises(self, tmp_path):
        # kb_root doesn't exist — record() is best-effort.
        record(
            tmp_path / "nonexistent", op="x",
            target="papers/X", actor="cli",
        )  # must not raise

    def test_extra_dict_merged(self, kb, monkeypatch):
        _clear_audit_env(monkeypatch)
        record(
            kb, op="x", target="papers/X", actor="cli",
            extra={"selector": "random"},
        )
        entry = _last_entry(kb)
        assert entry.get("selector") == "random"
