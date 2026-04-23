"""Tests for workspace autodetect from CWD.

Added in 0.27.3: autodetect now looks for `ee-kb/` from the
user's current directory, not just `.ee-kb-tools/` from the
code's install location. The `ee-kb/` directory is the stable
identifier of a KB workspace — it's named that way by
convention regardless of where the code lives. Autodetect from
CWD handles the common "user cd'd to workspace" flow without
needing env vars, independent of whether the source repo is
inside, beside, or elsewhere-from the workspace.

Previously, autodetect only succeeded when the code was
installed from inside `.ee-kb-tools/` (the deployed layout).
This was restrictive: a git-cloned source repo in
`~/dev/kb-tools/` with a KB in `~/research/ee-kb/` gave
"could not resolve workspace layout" every time unless the
user remembered to export `KB_ROOT`."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from kb_core.workspace import (
    find_workspace_root,
    resolve_workspace,
    WorkspaceError,
    KB_DIR_NAME,
    TOOLS_DIR_NAME,
)


class TestFindWorkspaceRoot:
    def test_cwd_is_workspace_parent(self, tmp_path):
        """User is in the directory that contains ee-kb/ — the
        expected day-to-day setup."""
        (tmp_path / "ee-kb").mkdir()
        got = find_workspace_root(start=tmp_path)
        assert got == tmp_path

    def test_cwd_is_ee_kb_itself(self, tmp_path):
        """User cd'd into their KB."""
        kb = tmp_path / "ee-kb"
        kb.mkdir()
        got = find_workspace_root(start=kb)
        assert got == tmp_path

    def test_cwd_is_deep_inside_ee_kb(self, tmp_path):
        """User is in papers/ or topics/. Walk up until we find
        the parent of ee-kb/."""
        papers = tmp_path / "ee-kb" / "papers"
        papers.mkdir(parents=True)
        got = find_workspace_root(start=papers)
        assert got == tmp_path

    def test_cwd_has_dot_ee_kb_tools_sibling(self, tmp_path):
        """Deployed layout — .ee-kb-tools/ next to ee-kb/. Either
        sibling is enough to identify the parent."""
        (tmp_path / ".ee-kb-tools").mkdir()
        got = find_workspace_root(start=tmp_path)
        assert got == tmp_path

    def test_dot_ee_kb_tools_alone_without_ee_kb(self, tmp_path):
        """Right after scripts/deploy.sh, before kb-write init —
        .ee-kb-tools/ exists, ee-kb/ doesn't yet. Autodetect
        should still return the parent so init can proceed."""
        (tmp_path / ".ee-kb-tools").mkdir()
        got = find_workspace_root(start=tmp_path)
        assert got == tmp_path

    def test_no_matching_ancestor(self, tmp_path):
        """User ran the CLI from some unrelated directory."""
        got = find_workspace_root(start=tmp_path)
        assert got is None

    def test_walks_up_to_find_match(self, tmp_path):
        """User is three dirs deep inside a workspace parent."""
        (tmp_path / "ee-kb").mkdir()
        deep = tmp_path / "ee-kb" / "papers" / "subgroup"
        deep.mkdir(parents=True)
        got = find_workspace_root(start=deep)
        assert got == tmp_path


class TestResolveWorkspace:
    def test_cwd_autodetect_succeeds(self, tmp_path, monkeypatch):
        """resolve_workspace should use CWD autodetect when no env
        var is set — the "just cd and run" flow."""
        (tmp_path / "ee-kb").mkdir()
        (tmp_path / "zotero").mkdir()
        # Clear env and CWD-mock.
        monkeypatch.delenv("KB_ROOT", raising=False)
        monkeypatch.delenv("KB_WORKSPACE", raising=False)
        monkeypatch.chdir(tmp_path)

        ws = resolve_workspace()
        assert ws.kb_root == (tmp_path / "ee-kb").resolve()
        assert ws.parent == tmp_path.resolve()

    def test_env_var_beats_cwd(self, tmp_path, monkeypatch):
        """$KB_ROOT should win over CWD autodetect."""
        # CWD workspace
        (tmp_path / "here").mkdir()
        (tmp_path / "here" / "ee-kb").mkdir()
        (tmp_path / "here" / "zotero").mkdir()
        # Env workspace
        (tmp_path / "there").mkdir()
        (tmp_path / "there" / "ee-kb").mkdir()
        (tmp_path / "there" / "zotero").mkdir()

        monkeypatch.delenv("KB_WORKSPACE", raising=False)
        monkeypatch.setenv("KB_ROOT", str(tmp_path / "there" / "ee-kb"))
        monkeypatch.chdir(tmp_path / "here")

        ws = resolve_workspace()
        assert ws.kb_root == (tmp_path / "there" / "ee-kb").resolve()

    def test_unreachable_parent_raises(self, tmp_path, monkeypatch):
        """No env var, no ee-kb/ anywhere on the CWD walk, no
        .ee-kb-tools/ ancestor of the code (doesn't apply in tests
        running from /tmp anyway). Must raise with a helpful
        message."""
        monkeypatch.delenv("KB_ROOT", raising=False)
        monkeypatch.delenv("KB_WORKSPACE", raising=False)
        monkeypatch.chdir(tmp_path)
        # Isolate from the runner's own install location — in
        # sandbox, kb_core itself may live under a `.ee-kb-tools/`
        # ancestor, which would let fallback #5 succeed with a
        # nonsensical parent. Force both autodetect paths to miss
        # so we exercise the real "no layout found" error.
        import kb_core.workspace as kbw
        monkeypatch.setattr(kbw, "find_tools_dir", lambda: None)

        with pytest.raises(WorkspaceError) as exc:
            resolve_workspace()

        msg = str(exc.value)
        # Error should be actionable — list the four ways to fix it.
        assert "cd" in msg.lower()
        assert "KB_ROOT" in msg
        assert "--kb-root" in msg
