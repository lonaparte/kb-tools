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

    # Stage.
    r = subprocess.run(
        ["git", "-C", str(kb_root), "add", "--"] + [str(f) for f in files],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise GitError(f"git add failed: {r.stderr.strip()}")

    return _commit_if_staged(
        kb_root, op=op, target=target, message_body=message_body,
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
) -> str | None:
    """Shared tail: check `git diff --cached --quiet`, commit if
    non-clean, return SHA."""
    # Check whether anything is actually staged.
    r = subprocess.run(
        ["git", "-C", str(kb_root), "diff", "--cached", "--quiet"],
        capture_output=True, text=True, check=False,
    )
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

    r = subprocess.run(
        ["git", "-C", str(kb_root), "commit", "-m", full_msg],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise GitError(f"git commit failed: {r.stderr.strip() or r.stdout.strip()}")

    # Grab the SHA.
    r = subprocess.run(
        ["git", "-C", str(kb_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()
