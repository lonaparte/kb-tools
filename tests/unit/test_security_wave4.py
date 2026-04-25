"""Locks in 1.4.3 wave-4 audit-response fixes:
  - kb-mcp PATH fallback default-deny (opt-in via env var)
  - --no-lock / --no-git-commit require KB_WRITE_ALLOW_UNSAFE_FLAGS=1
  - kb-importer --no-git-commit gated the same way
"""
from __future__ import annotations

import argparse


def _ns(**kw):
    """Build an argparse.Namespace with all expected kb-write fields."""
    return argparse.Namespace(
        no_lock=kw.get("no_lock", False),
        no_git_commit=kw.get("no_git_commit", False),
        no_reindex=kw.get("no_reindex", False),
    )


# ----------------------------------------------------------------------
# kb-write CLI gate
# ----------------------------------------------------------------------


def test_kb_write_unsafe_flag_gate_blocks_no_lock(monkeypatch):
    from kb_write.safety import _check_unsafe_flags

    monkeypatch.delenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", raising=False)
    args = _ns(no_lock=True)
    try:
        _check_unsafe_flags(args)
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--no-lock without opt-in must SystemExit")


def test_kb_write_unsafe_flag_gate_blocks_no_git_commit(monkeypatch):
    from kb_write.safety import _check_unsafe_flags

    monkeypatch.delenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", raising=False)
    args = _ns(no_git_commit=True)
    try:
        _check_unsafe_flags(args)
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--no-git-commit without opt-in must SystemExit")


def test_kb_write_unsafe_flag_gate_allows_with_env(monkeypatch):
    """Both unsafe flags pass when the env var is set to "1"."""
    from kb_write.safety import _check_unsafe_flags

    monkeypatch.setenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", "1")
    # Should not raise.
    _check_unsafe_flags(_ns(no_lock=True))
    _check_unsafe_flags(_ns(no_git_commit=True))
    _check_unsafe_flags(_ns(no_lock=True, no_git_commit=True))


def test_kb_write_unsafe_flag_gate_no_reindex_not_blocked(monkeypatch):
    """--no-reindex is intentionally NOT gated. Stale search is
    recoverable via `kb-mcp index`; concurrent-write corruption is
    not. Don't make the gate noisy for the recoverable case."""
    from kb_write.safety import _check_unsafe_flags

    monkeypatch.delenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", raising=False)
    # Should not raise / SystemExit.
    _check_unsafe_flags(_ns(no_reindex=True))


def test_kb_write_unsafe_flag_gate_clean_args_pass(monkeypatch):
    from kb_write.safety import _check_unsafe_flags

    monkeypatch.delenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", raising=False)
    _check_unsafe_flags(_ns())  # all defaults False — must not raise


def test_kb_write_unsafe_flag_gate_env_must_be_exactly_one(monkeypatch):
    """Loose truthy values (true / yes / TRUE) must NOT count as
    opt-in. The env var is intentionally a single literal "1" so
    accidental "export KB_WRITE_ALLOW_UNSAFE_FLAGS=true" doesn't
    silently grant the gate."""
    from kb_write.safety import _check_unsafe_flags

    for bad in ("", "0", "true", "yes", "TRUE", "Y"):
        monkeypatch.setenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", bad)
        try:
            _check_unsafe_flags(_ns(no_lock=True))
        except SystemExit:
            pass
        else:
            raise AssertionError(
                f"env={bad!r} should not satisfy the gate"
            )


# ----------------------------------------------------------------------
# kb-importer CLI gate
# ----------------------------------------------------------------------


def test_kb_importer_no_git_commit_gated(monkeypatch):
    from kb_importer.safety import _check_unsafe_flags as imp_check

    monkeypatch.delenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", raising=False)
    args = argparse.Namespace(no_git_commit=True)
    try:
        imp_check(args)
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError(
            "kb-importer --no-git-commit without opt-in must SystemExit"
        )


def test_kb_importer_no_git_commit_passes_with_env(monkeypatch):
    from kb_importer.safety import _check_unsafe_flags as imp_check

    monkeypatch.setenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", "1")
    imp_check(argparse.Namespace(no_git_commit=True))  # no raise


def test_kb_importer_clean_args_pass(monkeypatch):
    from kb_importer.safety import _check_unsafe_flags as imp_check

    monkeypatch.delenv("KB_WRITE_ALLOW_UNSAFE_FLAGS", raising=False)
    imp_check(argparse.Namespace(no_git_commit=False))


# ----------------------------------------------------------------------
# Cross-tool: env var name agreement
# ----------------------------------------------------------------------


def test_env_var_name_matches_across_tools():
    """Both tools must read the same env var so a single
    `export KB_WRITE_ALLOW_UNSAFE_FLAGS=1` covers a debugging
    session that hits both kb-write and kb-importer."""
    from kb_write.safety import _UNSAFE_FLAGS_OPT_IN_ENV as kw_name
    from kb_importer.safety import _UNSAFE_FLAGS_OPT_IN_ENV as ki_name
    assert kw_name == ki_name == "KB_WRITE_ALLOW_UNSAFE_FLAGS"
