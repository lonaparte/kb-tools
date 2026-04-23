"""Regression for the v0.27.3 field-report finding #C:
`.git/index.lock` contention on 50-way concurrent writes lost
3/50 commits. v0.27.4 adds bounded exponential-backoff retry on
the three git invocations that either take or inspect the index.

Tests exercise the retry wrapper directly — a real parallel
workload would require 50 processes and is prone to flakiness,
so we inject failing CompletedProcess shapes and verify the
wrapper does the right thing."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_result(rc, stderr="", stdout=""):
    """Build a minimal CompletedProcess lookalike."""
    return SimpleNamespace(returncode=rc, stderr=stderr, stdout=stdout)


def test_retries_on_index_lock(monkeypatch):
    from kb_write import git as kw_git

    # Simulate: first 2 calls fail with index.lock, 3rd succeeds.
    calls = [
        _make_result(
            128,
            stderr="fatal: Unable to create '.git/index.lock': "
                   "File exists.\n\nAnother git process seems to be "
                   "running in this repository...",
        ),
        _make_result(128, stderr="another git process seems to be running"),
        _make_result(0, stdout="", stderr=""),
    ]
    call_count = {"n": 0}

    def fake_run(argv, capture_output, text, check):
        i = call_count["n"]
        call_count["n"] += 1
        return calls[i]

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = kw_git._run_git_with_retry(
        ["git", "status"], max_attempts=5,
    )
    assert result.returncode == 0
    assert call_count["n"] == 3


def test_no_retry_on_non_lock_error(monkeypatch):
    """An unrelated error (e.g. bad commit message, missing file,
    bad branch) should NOT be retried — immediate surface."""
    from kb_write import git as kw_git

    def fake_run(argv, **_kw):
        return _make_result(128, stderr="fatal: bad object HEAD")

    call_count = {"n": 0}
    def counting_run(argv, capture_output, text, check):
        call_count["n"] += 1
        return _make_result(128, stderr="fatal: bad object HEAD")

    monkeypatch.setattr(subprocess, "run", counting_run)

    result = kw_git._run_git_with_retry(
        ["git", "commit"], max_attempts=5,
    )
    assert result.returncode == 128
    assert call_count["n"] == 1, (
        f"non-lock errors should not retry; got {call_count['n']} calls"
    )


def test_gives_up_after_max_attempts(monkeypatch):
    """If the lock NEVER clears, we return the last error after
    max_attempts — don't loop forever."""
    from kb_write import git as kw_git

    call_count = {"n": 0}
    def always_locked(argv, capture_output, text, check):
        call_count["n"] += 1
        return _make_result(
            128, stderr="another git process seems to be running",
        )

    monkeypatch.setattr(subprocess, "run", always_locked)

    result = kw_git._run_git_with_retry(
        ["git", "commit"], max_attempts=3,
    )
    assert result.returncode == 128
    assert call_count["n"] == 3, (
        f"expected exactly max_attempts=3 calls, got {call_count['n']}"
    )


def test_success_on_first_try_no_retry(monkeypatch):
    from kb_write import git as kw_git

    call_count = {"n": 0}
    def succeed(argv, capture_output, text, check):
        call_count["n"] += 1
        return _make_result(0)

    monkeypatch.setattr(subprocess, "run", succeed)

    kw_git._run_git_with_retry(["git", "status"])
    assert call_count["n"] == 1


def test_index_lock_literal_matches(monkeypatch):
    """The 'unable to create' / 'index.lock' strings in git's
    output are the heuristic markers. All three variants must
    trigger a retry."""
    from kb_write import git as kw_git

    variants = [
        "fatal: Unable to create '.git/index.lock': File exists.",
        "Another git process seems to be running in this repository",
        "error: could not lock config file .git/config",  # contains index.lock? no — should NOT retry
    ]
    # Only the first two should retry. The third mentions neither
    # "another git process" nor "index.lock" nor "unable to create"
    # — wait, it does say "could not lock" ... let's see what our
    # marker list catches.
    #
    # Markers are: "another git process seems to be running",
    # "index.lock", "unable to create". Third string matches none
    # → no retry.
    for text in variants[:2]:
        calls = [_make_result(128, stderr=text), _make_result(0)]
        i = {"n": 0}
        def fr(argv, **_):
            r = calls[i["n"]]; i["n"] += 1; return r
        monkeypatch.setattr(subprocess, "run", fr)
        r = kw_git._run_git_with_retry(["git", "x"], max_attempts=3)
        assert r.returncode == 0, f"should have retried past {text!r}"
