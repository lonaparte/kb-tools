"""Delete operations for agent-owned nodes (thought, topic, preference).

Papers and notes are NEVER deletable through kb-write — they live
and die with Zotero. To remove a paper, remove it from Zotero, then
run `kb-importer check-orphans` / kb-importer's archive flow.

Deletion behaviour depends on `ctx.git_commit`:

- `git_commit=True` and repo is a git checkout:
    `git rm --force <path>` stages the deletion, then commit_staged()
    writes a commit so the deletion appears cleanly in history.

- `git_commit=False`, OR the kb is not a git checkout at all:
    plain `Path.unlink()`. We deliberately do NOT run `git rm` in
    this branch — previously we did, which left a staged deletion in
    the user's index even though they asked for no git side effects
    (and a subsequent manual `git commit` would have silently picked
    it up). The contract for `git_commit=False` is "no git state
    mutation", and we now honour it.

The `confirm=True` arg is required on the Python API; the CLI
requires `--yes`. This catches agent typos and accidental mass-
deletes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..atomic import write_lock
from ..config import WriteContext
from ..git import auto_commit, commit_staged, is_git_repo
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import RuleViolation
from .thought import WriteResult, _nullcontext, _record_audit


DELETABLE_TYPES = {"thought", "topic", "preference"}


def delete(
    ctx: WriteContext,
    target: str | NodeAddress,
    *,
    confirm: bool,
) -> WriteResult:
    """Delete the md file for `target`.

    Args:
        ctx: WriteContext.
        target: str path ("thoughts/SLUG") or NodeAddress.
        confirm: must be True. This is a guard against AI
                 accidentally calling delete() — the Python API
                 rejects confirm=False / missing-default. CLI
                 enforces via --yes.

    Raises:
        RuleViolation: wrong node type, or confirm != True.
        FileNotFoundError: target missing.
    """
    if not confirm:
        raise RuleViolation(
            "delete() requires confirm=True. This is an intentional "
            "guard against unintended deletion."
        )

    # Handle the preference pseudo-type separately since it doesn't
    # go through parse_target.
    if isinstance(target, str) and target.startswith(".agent-prefs/"):
        # Convert to NodeAddress("preference", slug).
        tail = target[len(".agent-prefs/"):]
        if tail.endswith(".md"):
            tail = tail[:-3]
        # Defense-in-depth: validate the slug the same way create/update
        # do, so a caller who constructs target="...prefs/../../outside"
        # can't sneak `..` past the delete path. Previously this
        # branch skipped slug validation entirely — rejected by
        # validate_topic_slug (which delete will call below via
        # NodeAddress → md_abspath) only because that regex happens
        # to reject `..`, but relying on implicit downstream rejection
        # is fragile; the other two prefix paths (create, update) both
        # call _validate_pref_slug explicitly, and delete should too.
        from .preference import _validate_pref_slug
        _validate_pref_slug(tail)
        address = NodeAddress(node_type="preference", key=tail)
    elif isinstance(target, str) and "/" not in target:
        # Ambiguous bare key — require subdir prefix.
        raise RuleViolation(
            f"{target!r} is ambiguous. Prefix with 'thoughts/', "
            "'topics/', or '.agent-prefs/'."
        )
    else:
        address = target if isinstance(target, NodeAddress) else parse_target(str(target))

    if address.node_type not in DELETABLE_TYPES:
        raise RuleViolation(
            f"cannot delete {address.node_type!r}: only thought, "
            "topic, and preference are deletable via kb-write. "
            "Papers/notes are managed by kb-importer."
        )

    md_path = address.md_abspath(ctx.kb_root)
    # Hard boundary check: whatever path we built, it MUST live
    # inside kb_root. This catches any slug validation gap that
    # would otherwise let `target=".agent-prefs/../../outside"` or a
    # crafted NodeAddress escape the KB. md_abspath() calls resolve()
    # so symlink-based escapes are also flattened before this check.
    resolved_root = ctx.kb_root.resolve()
    try:
        md_path.relative_to(resolved_root)
    except ValueError:
        raise RuleViolation(
            f"delete target path {md_path} resolved outside kb_root "
            f"{resolved_root}. Refusing to delete — check target string "
            f"for '..' / symlink-based path traversal."
        )
    if not md_path.exists():
        raise FileNotFoundError(f"{md_path} does not exist")

    if ctx.dry_run:
        from ..diff import preview_delete
        old_text = md_path.read_text(encoding="utf-8")
        return WriteResult(
            address=address, md_path=md_path, mtime=0.0,
            preview=preview_delete(address.md_rel_path, old_text),
        )

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        # Two paths depending on whether we'll commit afterwards:
        #
        # - git_commit=True AND in a git repo: `git rm --force <path>`,
        #   which stages the deletion so the later commit picks it up.
        #
        # - git_commit=False (or not in a git repo): plain `unlink()`,
        #   DO NOT touch the index. Previously this branch always ran
        #   `git rm` when in a repo, which left a staged-deletion in
        #   the user's index even though they'd explicitly asked us
        #   not to commit. That's a silent side-effect: a subsequent
        #   manual commit by the user would pick up our deletion
        #   whether they wanted it or not. Contract for git_commit=
        #   False is "no git side effects", and we now honour it.
        deletion_staged = False
        if ctx.git_commit and is_git_repo(ctx.kb_root):
            r = subprocess.run(
                ["git", "-C", str(ctx.kb_root), "rm", "--force", str(md_path)],
                capture_output=True, text=True, check=False,
            )
            if r.returncode == 0:
                deletion_staged = True
            else:
                # `git rm` failed. Before falling back to plain unlink,
                # classify the failure — only a narrow set of failures
                # are safe to paper over. Previously this branch
                # silently swallowed the error and called unlink(),
                # which could leave the index and working tree out of
                # sync (e.g. file removed from disk but still staged,
                # or still in index pointing at stale content). That
                # corrupts subsequent commits in subtle ways.
                stderr = (r.stderr or "").strip()
                stderr_lc = stderr.lower()
                # Safe: file isn't tracked by git at all. `git rm`
                # refuses, plain unlink is the right thing.
                safe = (
                    "did not match any files" in stderr_lc
                    or "pathspec" in stderr_lc and "did not match" in stderr_lc
                )
                if not safe:
                    from ..git import GitError
                    raise GitError(
                        f"git rm failed for {address.md_rel_path} "
                        f"(exit {r.returncode}): {stderr or '(no stderr)'}. "
                        f"Refusing to unlink to avoid index/worktree "
                        f"mismatch. Resolve the git state manually "
                        f"(e.g. `git status`, `git reset`) and retry."
                    )
            # After a *safe* git rm failure (or a success that somehow
            # still left the file), fall back to plain unlink.
            if md_path.exists():
                md_path.unlink()
        else:
            # Either git_commit=False or not a git repo at all.
            md_path.unlink()

        sha = None
        if ctx.git_commit:
            if deletion_staged:
                # `git rm` already staged; just commit what's in the index.
                # Calling `git add <deleted_path>` here would fail with
                # "pathspec did not match any files".
                sha = commit_staged(
                    ctx.kb_root,
                    op=f"delete_{address.node_type}",
                    target=address.md_rel_path,
                    message_body=ctx.commit_message,
                    enabled=True,
                )
            else:
                # Non-git path: file was just unlinked; nothing to commit.
                # (is_git_repo was False above, so auto_commit is a no-op
                # anyway, but keep this explicit for clarity.)
                pass
        # Reindex so kb-mcp drops the orphan.
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op=f"delete_{address.node_type}",
        target=address.md_rel_path,
        mtime_after=0.0, git_sha=sha, reindexed=reindexed,
    )

    # mtime=0.0 since file no longer exists.
    return WriteResult(
        address=address, md_path=md_path, mtime=0.0,
        git_sha=sha, reindexed=reindexed,
    )
