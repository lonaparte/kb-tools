"""Minimal git wrapper for auto-commit after writes.

We shell out to the `git` binary rather than using a library because:
- No new Python dep.
- If git isn't installed, the absence is easy to detect and degrade
  on.
- The commit commands we run are trivial (add + commit).

By default, `kb-write` auto-commits after every successful write
(see AGENT-WRITE-RULES §6). Users can disable per-call or globally.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)


class GitError(Exception):
    pass


# v0.27.4: bounded retry-with-backoff for git operations that can
# race on .git/index.lock. When multiple kb-write processes commit
# concurrently against the same repo, each takes the index lock for
# its `add` + `commit` window. Git doesn't queue lock acquisition —
# the loser sees "Another git process seems to be running in this
# repository" and exits 128. At 50-way concurrency the collision
# rate is small but non-zero (field report: 3/50 commits lost even
# though all 50 md writes landed on disk).
#
# v0.27.5: also retry on `cannot lock ref 'HEAD'` errors. git holds
# TWO serialising locks during a commit — the index lock
# (.git/index.lock) during `add`/`commit`-staging and the HEAD ref
# lock (.git/HEAD.lock, or .git/refs/heads/<br>.lock) during the
# final ref update. Field report at 100-way concurrency saw the
# HEAD-ref lock fire separately from index.lock, and the prior
# retry set missed it (error text is "cannot lock ref 'HEAD': is
# at <sha> but expected <sha>"). Same retry strategy — git's own
# ref-lock window is shorter than the index-lock window, so the
# existing backoff schedule is already generous enough.
#
# Retry caps at ~1s total (0.05 + 0.1 + 0.2 + 0.4 = 0.75s) which is
# enough to outwait a typical commit (~20ms for a single-file
# staging + commit on SSD) but short enough that a truly-stuck lock
# file (crashed git process) surfaces promptly.
_LOCK_ERROR_MARKERS = (
    "another git process seems to be running",
    "index.lock",
    "unable to create",
    # v0.27.5: HEAD ref lock (separate code path from index.lock).
    "cannot lock ref",
    # Also the lower-level phrasing git sometimes emits when the
    # loose-ref file lock file itself (`<refpath>.lock`) can't be
    # taken — belt-and-braces so both phrasings are covered.
    "ref lock",
)


def _run_git_with_retry(argv: list[str], *, max_attempts: int = 5):
    """Run a git subprocess, retrying on .git/index.lock contention.

    Returns the CompletedProcess (caller inspects returncode). Any
    non-lock error returns after the first attempt. Lock errors
    retry with exponential backoff capped by max_attempts.
    """
    import time

    last = None
    delay = 0.05
    for attempt in range(max_attempts):
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
        last = r
        if r.returncode == 0:
            return r
        # Only retry on index.lock contention.
        err_low = (r.stderr + r.stdout).lower()
        if not any(m in err_low for m in _LOCK_ERROR_MARKERS):
            return r
        if attempt == max_attempts - 1:
            return r
        time.sleep(delay)
        delay = min(delay * 2, 0.5)
    return last


def is_git_repo(kb_root: Path) -> bool:
    """Check whether kb_root is (or is inside) a git repository."""
    try:
        r = subprocess.run(
            ["git", "-C", str(kb_root), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=False,
        )
        return r.returncode == 0
    except FileNotFoundError:
        # `git` binary missing.
        return False


def auto_commit(
    kb_root: Path,
    files: Sequence[Path],
    op: str,
    target: str,
    *,
    message_body: str | None = None,
    enabled: bool = True,
) -> str | None:
    """Stage `files` and commit with a structured message.

    Args:
        kb_root: repo root.
        files: paths to `git add`. May be absolute or relative; git
            doesn't mind.
        op: operation name, e.g. "create_thought", "update_topic".
            Appears first in commit subject.
        target: what was operated on, e.g. "thoughts/2026-04-22-idea".
        message_body: optional multi-line body appended after a
            blank line in the commit message.
        enabled: if False, no-op (return None). Used so callers can
            pass `git_commit=False` through.

    Returns the commit SHA on success, None if skipped or failed
    non-fatally (e.g. not a git repo — we don't want to block writes
    just because the KB isn't under version control).

    Raises GitError for genuine git failures (e.g. merge conflict,
    pre-commit hook failure) so the caller can surface the issue.

    For operations that have **already staged** their changes (e.g.
    `git rm` for delete), use `commit_staged()` instead — this
    function will fail because `git add` can't re-stage a file that
    no longer exists on disk.
    """
    if not enabled:
        return None
    if not is_git_repo(kb_root):
        log.info("kb_root %s is not a git repo; skipping auto-commit.", kb_root)
        return None
    if not files:
        return None

    # Stage. Goes through the retry wrapper — `git add` takes the
    # index lock too, and on a busy repo is the more common point
    # to collide since `add` happens before `commit`.
    file_args = [str(f) for f in files]
    r = _run_git_with_retry(
        ["git", "-C", str(kb_root), "add", "--"] + file_args,
    )
    if r.returncode != 0:
        raise GitError(f"git add failed: {r.stderr.strip()}")

    # v0.27.5: pass the file list as a pathspec to the commit step.
    # Without it, `git commit` commits the WHOLE index, so at high
    # concurrency process B's commit sweeps process A's staged
    # files into B's commit — leaving A's later `git commit` with
    # "nothing added to commit" even though A's md is on disk
    # (field report: 68/100 at 100-way --no-lock saw this). With
    # pathspec, each process commits ONLY the files it passed in,
    # so the worst case now is "the other process ran concurrently
    # and already committed my file" — which is a silent no-op
    # (md on disk + in git, just under the other process's commit
    # subject) rather than an error.
    return _commit_if_staged(
        kb_root, op=op, target=target, message_body=message_body,
        files=file_args,
    )


def commit_staged(
    kb_root: Path,
    op: str,
    target: str,
    *,
    message_body: str | None = None,
    enabled: bool = True,
) -> str | None:
    """Commit whatever is already staged in the index.

    Use this after operations that stage their own changes — the main
    case is `git rm` for delete, which both removes the file and
    stages the removal. Calling `auto_commit` on the already-deleted
    path fails because `git add <nonexistent>` errors.

    Returns SHA, or None if repo is clean / not a repo / disabled.
    """
    if not enabled:
        return None
    if not is_git_repo(kb_root):
        log.info("kb_root %s is not a git repo; skipping auto-commit.", kb_root)
        return None

    return _commit_if_staged(
        kb_root, op=op, target=target, message_body=message_body,
    )


def _commit_if_staged(
    kb_root: Path,
    *,
    op: str,
    target: str,
    message_body: str | None,
    files: Sequence[str] | None = None,
) -> str | None:
    """Shared tail: check `git diff --cached --quiet`, commit if
    non-clean, return SHA.

    If `files` is provided, both the staged-check and the commit
    are scoped to that pathspec — so concurrent auto_commits on
    disjoint files don't accidentally sweep each other's staged
    changes into one commit. `commit_staged()` (used by delete)
    passes None to keep the historical "commit whatever's staged"
    behaviour.
    """
    # Check whether anything is actually staged in this process's
    # pathspec. Uses the retry wrapper — `git diff --cached`
    # doesn't itself take the index lock but reads the index, and
    # a mid-commit race can briefly surface transient errors.
    diff_argv = ["git", "-C", str(kb_root), "diff", "--cached", "--quiet"]
    if files:
        diff_argv += ["--"] + list(files)
    r = _run_git_with_retry(diff_argv)
    if r.returncode == 0:
        log.debug("auto_commit: nothing to commit for %s.", target)
        return None

    # Compose message.
    subject = f"{op}: {target} [kb-write]"
    msg_parts = [subject]
    if message_body:
        msg_parts.append("")
        msg_parts.append(message_body.rstrip())
    full_msg = "\n".join(msg_parts)

    commit_argv = ["git", "-C", str(kb_root), "commit", "-m", full_msg]
    if files:
        commit_argv += ["--"] + list(files)
    r = _run_git_with_retry(commit_argv)
    if r.returncode != 0:
        # v0.27.5: at very high concurrency, a sibling process can
        # stage+commit our file between our diff-cached check and
        # our commit call. `git commit -- <pathspec>` then exits
        # non-zero with "nothing to commit" / "nothing added to
        # commit" — the md is on disk AND already in git under
        # the other process's commit, so this is not a data-loss
        # failure. Swallow quietly and return None.
        err_low = (r.stderr + r.stdout).lower()
        if (
            "nothing to commit" in err_low
            or "nothing added to commit" in err_low
            or "no changes added to commit" in err_low
        ):
            log.debug(
                "auto_commit: %s already committed by a sibling "
                "process — skipping.", target,
            )
            return None
        raise GitError(f"git commit failed: {r.stderr.strip() or r.stdout.strip()}")

    # Grab the SHA.
    r = subprocess.run(
        ["git", "-C", str(kb_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()
