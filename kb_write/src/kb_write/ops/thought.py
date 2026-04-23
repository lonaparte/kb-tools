"""Thought operations.

A thought is a dated Markdown note under `thoughts/`. The whole file
is owned by the agent/user. Typical use: record a cross-paper
insight, capture a half-formed idea, annotate a reading session.

Two operations:

- `create(ctx, title, body, slug=None, refs=[], tags=[], ...)` — new file
- `update(ctx, address, expected_mtime, body=None, title=None, refs=None, tags=None)` — edit

Both return a `WriteResult` carrying the final path, mtime, and git
SHA (if auto-commit kicked in).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from ..atomic import write_lock
from ..config import WriteContext
from ..frontmatter import read_md, write_md, merge_kb_fields
from ..git import auto_commit
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import (
    RuleViolation,
    make_thought_slug,
    validate_kb_ref_entry,
    validate_thought_slug,
)


@dataclass
class WriteResult:
    """What a write op returns to the caller.

    When ctx.dry_run is True, `mtime` is 0.0 and `diff` / `preview`
    carry the would-be change so the caller (CLI, MCP wrapper) can
    surface it.
    """
    address: NodeAddress
    md_path: Path
    mtime: float
    git_sha: str | None = None
    reindexed: bool = False
    # dry-run extras
    diff: str = ""            # unified diff, empty if create/delete
    preview: str = ""         # human-readable summary for create/delete


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------

def create(
    ctx: WriteContext,
    title: str,
    body: str,
    *,
    slug: str | None = None,
    refs: Iterable[str] = (),
    tags: Iterable[str] = (),
    extra_frontmatter: dict | None = None,
) -> WriteResult:
    """Create a new thought.

    Args:
        ctx: WriteContext with kb_root, git_commit flag, etc.
        title: human-readable title (goes into frontmatter).
        body: md body (everything after frontmatter).
        slug: if provided, must pass validate_thought_slug. If None,
              auto-generated from title + today's date.
        refs: kb_refs entries. Each must pass validate_kb_ref_entry.
        tags: kb_tags entries. No validation beyond being strings.
        extra_frontmatter: additional kb_* fields to set. Protected
              fields (zotero_*, fulltext_*, kind, title, etc.) in
              this dict are silently dropped by merge_kb_fields.

    Raises:
        RuleViolation: slug or refs malformed.
        WriteExistsError: target path already has a file.
    """
    if not title.strip():
        raise RuleViolation("thought title cannot be empty")

    final_slug = slug or make_thought_slug(title, today=date.today())
    validate_thought_slug(final_slug)

    refs_list = [str(r).strip() for r in refs if str(r).strip()]
    for r in refs_list:
        validate_kb_ref_entry(r)
    tags_list = [str(t).strip() for t in tags if str(t).strip()]

    address = NodeAddress("thought", final_slug)
    target = address.md_abspath(ctx.kb_root)

    # Build frontmatter.
    fm: dict = {
        "kind": "thought",
        "title": title.strip(),
        "created_at": date.today().isoformat(),
        "kb_tags": tags_list,
        "kb_refs": refs_list,
    }
    if extra_frontmatter:
        fm = merge_kb_fields(fm, extra_frontmatter)

    if ctx.dry_run:
        # Assemble what the file would look like for preview.
        try:
            import frontmatter as _fm_mod
            post = _fm_mod.Post(body, **fm)
            new_text = _fm_mod.dumps(post)
            if not new_text.endswith("\n"):
                new_text += "\n"
        except Exception:
            new_text = body
        from ..diff import preview_create
        return WriteResult(
            address=address,
            md_path=target,
            mtime=0.0,
            preview=preview_create(address.md_rel_path, new_text),
        )

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        mtime = write_md(target, fm, body, expected_mtime=None, create_only=True)

        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [target],
                op="create_thought",
                target=address.md_rel_path,
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    if ctx.audit:
        _record_audit(
            ctx, op="create_thought",
            target=address.md_rel_path,
            mtime_after=mtime, git_sha=sha, reindexed=reindexed,
            extra={"refs": refs_list, "tags": tags_list},
        )

    return WriteResult(
        address=address,
        md_path=target,
        mtime=mtime,
        git_sha=sha,
        reindexed=reindexed,
    )


# ---------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------

def update(
    ctx: WriteContext,
    target: str | NodeAddress,
    expected_mtime: float,
    *,
    body: str | None = None,
    title: str | None = None,
    refs: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    extra_frontmatter: dict | None = None,
    refs_mode: str = "replace",
    tags_mode: str = "replace",
) -> WriteResult:
    """Update an existing thought. Only the fields you pass are
    modified; unspecified fields are preserved.

    Args:
        ctx: WriteContext.
        target: address of the thought (str "thoughts/SLUG" or
                NodeAddress).
        expected_mtime: mtime from the read that produced the
                content you're basing this update on. Required.
        body: if not None, replaces the body entirely.
        title: if not None, updates frontmatter title.
        refs / tags: if not None, applied per refs_mode / tags_mode:
            "replace" — set the list to exactly this (filtered).
            "add"     — merge-union with existing.
            "remove"  — remove these items from existing.
        extra_frontmatter: kb_* fields to overlay (protected fields
                silently dropped).

    Raises:
        RuleViolation, WriteConflictError, FrontmatterError.
    """
    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    if address.node_type != "thought":
        raise RuleViolation(
            f"update() on a {address.node_type}; use the correct ops module."
        )
    validate_thought_slug(address.key)

    md_path = address.md_abspath(ctx.kb_root)
    existing_fm, existing_body, _actual_mtime = read_md(md_path)

    # Compose new frontmatter.
    new_fm = dict(existing_fm)
    if title is not None:
        new_fm["title"] = title.strip()

    if refs is not None:
        refs_list = [str(r).strip() for r in refs if str(r).strip()]
        for r in refs_list:
            validate_kb_ref_entry(r)
        new_fm["kb_refs"] = _apply_list_mode(
            existing_fm.get("kb_refs") or [], refs_list, refs_mode,
        )

    if tags is not None:
        tags_list = [str(t).strip() for t in tags if str(t).strip()]
        new_fm["kb_tags"] = _apply_list_mode(
            existing_fm.get("kb_tags") or [], tags_list, tags_mode,
        )

    if extra_frontmatter:
        new_fm = merge_kb_fields(new_fm, extra_frontmatter)

    new_body = existing_body if body is None else body

    if ctx.dry_run:
        # Compose what the new file would be, render diff.
        try:
            import frontmatter as _fm_mod
            old_text = md_path.read_text(encoding="utf-8")
            post = _fm_mod.Post(new_body, **new_fm)
            new_text = _fm_mod.dumps(post)
            if not new_text.endswith("\n"):
                new_text += "\n"
            from ..diff import make_diff
            diff = make_diff(old_text, new_text, path=address.md_rel_path)
        except Exception as e:
            diff = f"(dry-run diff unavailable: {e})"
        return WriteResult(
            address=address, md_path=md_path, mtime=0.0, diff=diff,
        )

    # No-op detection: if the composed (frontmatter, body) matches
    # what's already on disk, don't rewrite. Avoids bumping mtime
    # (which in turn avoids false-positive reindex / git-commit
    # noise, and reduces the race-conflict surface against other
    # writers). We still honour expected_mtime so callers holding
    # a stale mtime get a conflict error rather than silent skip.
    # Body compared after rstrip to tolerate trailing-newline
    # differences between what the caller passed and what was
    # serialised on disk last time.
    if (new_fm == existing_fm
            and new_body.rstrip() == existing_body.rstrip()):
        if expected_mtime is not None:
            from ..atomic import assert_mtime_unchanged
            assert_mtime_unchanged(md_path, expected_mtime)
        return WriteResult(
            address=address, md_path=md_path,
            mtime=md_path.stat().st_mtime,
            git_sha=None, reindexed=False,
        )

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        mtime = write_md(
            md_path, new_fm, new_body,
            expected_mtime=expected_mtime,
        )
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="update_thought",
                target=address.md_rel_path,
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    if ctx.audit:
        _record_audit(
            ctx, op="update_thought",
            target=address.md_rel_path,
            mtime_before=expected_mtime, mtime_after=mtime,
            git_sha=sha, reindexed=reindexed,
        )

    return WriteResult(
        address=address,
        md_path=md_path,
        mtime=mtime,
        git_sha=sha,
        reindexed=reindexed,
    )


def _apply_list_mode(existing: list, incoming: list, mode: str) -> list:
    if mode == "replace":
        # De-dupe while preserving order.
        seen = set()
        out = []
        for x in incoming:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out
    if mode == "add":
        seen = set(existing)
        out = list(existing)
        for x in incoming:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    if mode == "remove":
        drop = set(incoming)
        return [x for x in existing if x not in drop]
    raise RuleViolation(f"unknown list mode {mode!r}; expected replace|add|remove")


def _record_audit(ctx, **kwargs) -> None:
    """Shared audit hook used by every op module. No-op if
    ctx.audit is False (tests may disable). Import locally to avoid
    circular imports with ops package."""
    if not getattr(ctx, "audit", True):
        return
    from ..audit import record as _r
    _r(ctx.kb_root, actor=ctx.actor, **kwargs)


# nullcontext shim (avoid importing from contextlib if not needed for
# the lock-disabled path — keeps import graph lean).
class _nullcontext:
    def __enter__(self): return self
    def __exit__(self, *a): return False
