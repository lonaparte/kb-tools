"""Regression for the v0.27.4 field-report "nothing added to
commit" race under 100-way concurrent `kb-write thought create
--no-lock`.

The old shape of `auto_commit` / `_commit_if_staged` was:
    git add file_me
    git diff --cached --quiet            # any-file check
    git commit -m MSG                    # ALL staged files

so if sibling process S ran `git add file_S` + `git commit` in
between our `add` and `commit`, our final commit would find an
empty index and exit 128 with "nothing added to commit" — even
though our md was already on disk and in git (under S's
commit).

v0.27.5 passes the per-call file list as a pathspec to BOTH
`diff --cached --quiet -- file_me` and `git commit -m MSG --
file_me`. The two added behaviours:

1. Commits are now scoped — S and our process produce separate
   commits even when their adds interleave.
2. If S has already committed our file (so `git commit -- file_me`
   exits "nothing to commit"), we swallow that as a silent
   no-op (return None) rather than raising GitError.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace


def _make_result(rc, stderr="", stdout=""):
    return SimpleNamespace(returncode=rc, stderr=stderr, stdout=stdout)


def test_commit_scopes_to_pathspec(monkeypatch, tmp_path):
    """auto_commit must pass the file list as a pathspec to
    `git commit -- <file>`, not bare `git commit`."""
    from kb_write import git as kw_git

    # is_git_repo returns True (pretend kb_root is a git repo).
    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    captured: list[list[str]] = []

    def fake_run(argv, capture_output, text, check):
        captured.append(list(argv))
        # git add / diff / commit / SHA-lookup: return rc=0 for add
        # + commit, rc=1 for diff (meaning "something staged") so we
        # proceed to commit.
        if "diff" in argv:
            return _make_result(1)  # non-zero = something staged
        # 1.4.2: SHA lookup is `git log -1 --format=%H -- <pathspec>`
        # when files is set, falling back to rev-parse HEAD when not.
        if "rev-parse" in argv or ("log" in argv and "--format=%H" in argv):
            return _make_result(0, stdout="abc1234\n")
        return _make_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    target_file = tmp_path / "thoughts" / "t.md"
    sha = kw_git.auto_commit(
        tmp_path, [target_file], op="create_thought",
        target="thoughts/t", message_body=None,
    )
    assert sha == "abc1234"

    # The diff call must have `--` + the file as pathspec.
    diff_calls = [a for a in captured if "diff" in a]
    assert diff_calls, "diff --cached --quiet was never called"
    for argv in diff_calls:
        assert "--" in argv, f"diff call missing -- pathspec separator: {argv}"
        assert str(target_file) in argv, (
            f"diff call missing target file in pathspec: {argv}"
        )

    # The commit call must have `--` + the file as pathspec too.
    # 1.4.2: argv now includes `-c core.hooksPath=…` between `-C
    # <root>` and the subcommand, so we can't rely on a fixed index
    # for "commit". Locate by membership instead.
    commit_calls = [
        a for a in captured
        if "commit" in a and "diff" not in a and "log" not in a
    ]
    assert commit_calls, "git commit was never called"
    for argv in commit_calls:
        assert "--" in argv, f"commit missing -- pathspec separator: {argv}"
        assert str(target_file) in argv, (
            f"commit missing target file: {argv}"
        )


def test_commit_swallows_nothing_added_race(monkeypatch, tmp_path):
    """If a sibling process commits our file between our add and
    commit, `git commit -- file` exits non-zero with 'nothing to
    commit'. That's a silent no-op (return None), not an error —
    the md is on disk and already in git."""
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    call_log: list[list[str]] = []

    def fake_run(argv, capture_output, text, check):
        call_log.append(list(argv))
        if "add" in argv and "diff" not in argv:
            return _make_result(0)  # our own add succeeds
        if "diff" in argv:
            return _make_result(1)  # something staged (we think)
        if "commit" in argv:
            # Sibling swept our file into their commit before our
            # commit ran. git's real error text here is:
            return _make_result(
                1,
                stdout=(
                    "On branch main\n"
                    "nothing to commit, working tree clean\n"
                ),
            )
        if "rev-parse" in argv:
            return _make_result(0, stdout="abc1234\n")
        return _make_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    target_file = tmp_path / "thoughts" / "t.md"
    sha = kw_git.auto_commit(
        tmp_path, [target_file], op="create_thought",
        target="thoughts/t", message_body=None,
    )
    # Not a failure — sibling already committed our file. Returns
    # None (same sentinel as "not a git repo" / "nothing to commit").
    assert sha is None


def test_commit_swallows_nothing_added_variant_phrasing(monkeypatch, tmp_path):
    """Newer git versions say 'nothing added to commit but
    untracked files present' (rc=1) — the real v0.27.4 field
    observation. Must also be silenced."""
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    def fake_run(argv, capture_output, text, check):
        if "add" in argv and "diff" not in argv:
            return _make_result(0)
        if "diff" in argv:
            return _make_result(1)
        if "commit" in argv:
            return _make_result(
                1,
                stdout=(
                    "On branch main\n"
                    "Untracked files:\n"
                    "  (use \"git add <file>...\" to include in what "
                    "will be committed)\n"
                    "\tthoughts/v275-ref-085-1072.md\n"
                    "\nnothing added to commit but untracked files "
                    "present (use \"git add\" to track)\n"
                ),
            )
        return _make_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    sha = kw_git.auto_commit(
        tmp_path, [tmp_path / "thoughts" / "t.md"],
        op="create_thought", target="thoughts/t",
        message_body=None,
    )
    assert sha is None


def test_commit_non_race_failure_still_raises(monkeypatch, tmp_path):
    """An actual git failure (e.g. pre-commit hook rejection) that
    is NOT the benign 'nothing to commit' race must still surface
    as GitError. Otherwise we'd silently lose real write failures."""
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    def fake_run(argv, capture_output, text, check):
        if "add" in argv and "diff" not in argv:
            return _make_result(0)
        if "diff" in argv:
            return _make_result(1)
        if "commit" in argv:
            return _make_result(
                1,
                stderr="error: pre-commit hook rejected: bad content",
            )
        return _make_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    import pytest
    with pytest.raises(kw_git.GitError) as exc_info:
        kw_git.auto_commit(
            tmp_path, [tmp_path / "x.md"],
            op="create_thought", target="thoughts/t",
            message_body=None,
        )
    assert "pre-commit hook" in str(exc_info.value)


def test_commit_staged_keeps_whole_index_behaviour(monkeypatch, tmp_path):
    """`commit_staged()` (used by delete — `git rm` already staged
    the removal) must continue to commit the whole index without
    a pathspec, since the caller didn't pass a file list."""
    from kb_write import git as kw_git

    monkeypatch.setattr(kw_git, "is_git_repo", lambda _p: True)

    captured: list[list[str]] = []

    def fake_run(argv, capture_output, text, check):
        captured.append(list(argv))
        if "diff" in argv:
            return _make_result(1)  # something staged
        if "rev-parse" in argv:
            return _make_result(0, stdout="deadbee\n")
        return _make_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    sha = kw_git.commit_staged(
        tmp_path, op="delete_thought", target="thoughts/gone",
    )
    assert sha == "deadbee"

    # commit call must NOT have a `--` pathspec when commit_staged
    # is the entry point — that's how delete stays correct.
    commit_calls = [a for a in captured if "commit" in a and "diff" not in a]
    assert commit_calls
    for argv in commit_calls:
        assert "--" not in argv, (
            f"commit_staged unexpectedly passed a pathspec: {argv}"
        )
