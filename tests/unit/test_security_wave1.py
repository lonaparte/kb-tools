"""Locks in 1.4.2 wave-1 security fixes:
  - kb-mcp executable resolved by absolute path (not bare PATH)
  - git invocations disable hooks by default
  - delete commit_staged scopes via pathspec
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace


# ----------------------------------------------------------------------
# A.1: kb-mcp resolution prefers workspace venv, falls back to current
# Python's bin dir, then PATH (with a logged warning).
# ----------------------------------------------------------------------


def test_resolve_kb_mcp_prefers_workspace_venv(tmp_path, monkeypatch):
    from kb_write.reindex import _resolve_kb_mcp

    # Lay out a fake workspace: kb_root + sibling .ee-kb-tools/.venv/bin/kb-mcp
    kb_root = tmp_path / "ee-kb"
    kb_root.mkdir()
    venv_bin = tmp_path / ".ee-kb-tools" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_kb_mcp = venv_bin / "kb-mcp"
    fake_kb_mcp.write_text("#!/bin/sh\necho fake\n")
    fake_kb_mcp.chmod(0o755)

    # Pretend a different kb-mcp is also on PATH; we should NOT pick it.
    other = tmp_path / "fake_path" / "kb-mcp"
    other.parent.mkdir()
    other.write_text("malicious")
    other.chmod(0o755)

    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: str(other))

    resolved = _resolve_kb_mcp(kb_root)
    assert resolved is not None
    # Must be the workspace one, NOT the PATH-shadowed one.
    assert Path(resolved).resolve() == fake_kb_mcp.resolve()


def test_resolve_kb_mcp_returns_absolute_path(tmp_path, monkeypatch):
    """When falling through to PATH, the returned path must be
    absolute so subprocess.run isn't subject to PATH mutations
    after resolution."""
    from kb_write.reindex import _resolve_kb_mcp

    kb_root = tmp_path / "ee-kb"
    kb_root.mkdir()
    fake = tmp_path / "system_bin" / "kb-mcp"
    fake.parent.mkdir()
    fake.write_text("ok")
    fake.chmod(0o755)

    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: str(fake))

    resolved = _resolve_kb_mcp(kb_root)
    assert resolved is not None
    assert os.path.isabs(resolved)


def test_resolve_kb_mcp_returns_none_when_absent(tmp_path, monkeypatch):
    from kb_write.reindex import _resolve_kb_mcp

    kb_root = tmp_path / "ee-kb"
    kb_root.mkdir()
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    # Also no python-bin-dir kb-mcp (if dev-venv has it, this test
    # is a no-op pass; real CI has none in setup-python's bin).
    import sys as _sys
    py_bin = Path(_sys.executable).parent
    if (py_bin / "kb-mcp").exists() or (py_bin / "kb-mcp.exe").exists():
        # Skip via assertion-as-pass for environments with kb-mcp
        # installed alongside Python.
        return
    assert _resolve_kb_mcp(kb_root) is None


# ----------------------------------------------------------------------
# A.2: git invocations include core.hooksPath=/dev/null by default.
# ----------------------------------------------------------------------


def test_git_argv_disables_hooks_by_default(tmp_path):
    from kb_write.git import _git_argv

    argv = _git_argv(tmp_path, "commit", "-m", "x")
    # Both -c and the hooks override must appear.
    assert "-c" in argv
    idx = argv.index("-c")
    assert argv[idx + 1].startswith("core.hooksPath=")
    # Subcommand still present.
    assert "commit" in argv


def test_git_argv_run_hooks_true_omits_override(tmp_path):
    from kb_write.git import _git_argv

    argv = _git_argv(tmp_path, "commit", "-m", "x", run_hooks=True)
    assert all(not a.startswith("core.hooksPath=") for a in argv)


def test_auto_commit_passes_hooks_disabled(monkeypatch, tmp_path):
    """End-to-end: auto_commit's actual subprocess argv must contain
    the hooksPath override."""
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    captured = []

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        if "diff" in argv:
            return SimpleNamespace(returncode=1, stderr="", stdout="")
        if "rev-parse" in argv or ("log" in argv and "--format=%H" in argv):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "thoughts" / "x.md"
    sha = kw_git.auto_commit(
        tmp_path, [target], op="create_thought",
        target="thoughts/x", message_body=None,
    )
    assert sha == "abc1234"
    # At least one of {add, diff, commit} must have hooksPath override.
    hook_disabled_calls = [
        a for a in captured
        if any(s.startswith("core.hooksPath=") for s in a)
    ]
    assert hook_disabled_calls, (
        f"no git invocation disabled hooks; captured: {captured}"
    )


# ----------------------------------------------------------------------
# A.3: commit_staged with files= scopes commit to pathspec.
# ----------------------------------------------------------------------


def test_commit_staged_scopes_to_pathspec(monkeypatch, tmp_path):
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    captured = []

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        if "diff" in argv:
            return SimpleNamespace(returncode=1, stderr="", stdout="")
        if "rev-parse" in argv or ("log" in argv and "--format=%H" in argv):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha = kw_git.commit_staged(
        tmp_path, op="delete_thought",
        target="thoughts/x.md",
        files=["thoughts/x.md"],
    )
    assert sha == "abc1234"
    # Diff and commit calls must include the pathspec.
    pathspec_calls = [
        a for a in captured
        if "thoughts/x.md" in a and "--" in a
    ]
    assert pathspec_calls, (
        "commit_staged didn't pass pathspec to git diff/commit"
    )


def test_commit_staged_no_files_uses_full_index(monkeypatch, tmp_path):
    """Legacy callers (none in current code, but the API permits it)
    can still pass files=None to get the historical 'commit whole
    index' behaviour. New code should NOT do this for delete; this
    test just pins the legacy contract so we don't accidentally
    refactor it away."""
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    captured = []

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        if "diff" in argv:
            return SimpleNamespace(returncode=1, stderr="", stdout="")
        if "rev-parse" in argv or ("log" in argv and "--format=%H" in argv):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha = kw_git.commit_staged(
        tmp_path, op="legacy_op", target="something",
        files=None,
    )
    assert sha == "abc1234"
    # No pathspec separator in diff/commit calls.
    diff_or_commit_calls = [
        a for a in captured
        if any(sub in a for sub in ("diff", "commit"))
        and "log" not in a
    ]
    for argv in diff_or_commit_calls:
        # `--` may or may not appear; the assertion is that no
        # specific file follows it.
        if "--" in argv:
            idx = argv.index("--")
            # Tail after `--` is empty: no pathspec was attached.
            assert argv[idx + 1:] == [], f"unexpected pathspec in {argv}"


# ----------------------------------------------------------------------
# B.1: import_lock no longer unlinks file on success.
# ----------------------------------------------------------------------


def test_import_lock_keeps_file_on_release(tmp_path):
    from kb_importer.import_lock import import_lock, IMPORT_LOCK_REL

    kb_root = tmp_path
    (kb_root / ".kb-mcp").mkdir()
    lock_path = kb_root / IMPORT_LOCK_REL

    with import_lock(kb_root):
        assert lock_path.exists()
        # Has PID payload.
        assert lock_path.stat().st_size > 0

    # After exit: file exists but is empty.
    assert lock_path.exists()
    assert lock_path.stat().st_size == 0
