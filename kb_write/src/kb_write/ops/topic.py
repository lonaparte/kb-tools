"""Topic operations.

A topic is an organizational page under `topics/`. Agent/user owned.
Unlike thoughts, topics have no date prefix — their slug is an
identifier for the topic itself (e.g. `gfm-stability`,
`attention/overview`).

Near-duplicate of thought.py by design — the node-type-specific
invariants (no date prefix; hierarchical slug allowed) are the only
differences.
"""
from __future__ import annotations

from typing import Iterable

from ..atomic import write_lock
from ..config import WriteContext
from ..frontmatter import read_md, write_md, merge_kb_fields
from ..git import auto_commit
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import (
    RuleViolation,
    validate_kb_ref_entry,
    validate_topic_slug,
)
from .thought import WriteResult, _apply_list_mode, _nullcontext, _record_audit


def create(
    ctx: WriteContext,
    slug: str,
    title: str,
    body: str,
    *,
    refs: Iterable[str] = (),
    tags: Iterable[str] = (),
    extra_frontmatter: dict | None = None,
) -> WriteResult:
    """Create a new topic.

    `slug` is REQUIRED for topics (unlike thoughts, we can't
    synthesize a sensible one from a title — topics are named
    deliberately).
    """
    if not slug or not slug.strip():
        raise RuleViolation("topic slug cannot be empty")
    if not title.strip():
        raise RuleViolation("topic title cannot be empty")
    validate_topic_slug(slug)

    refs_list = [str(r).strip() for r in refs if str(r).strip()]
    for r in refs_list:
        validate_kb_ref_entry(r)
    tags_list = [str(t).strip() for t in tags if str(t).strip()]

    address = NodeAddress("topic", slug)
    target = address.md_abspath(ctx.kb_root)

    fm: dict = {
        "kind": "topic",
        "title": title.strip(),
        "kb_tags": tags_list,
        "kb_refs": refs_list,
    }
    if extra_frontmatter:
        fm = merge_kb_fields(fm, extra_frontmatter)

    if ctx.dry_run:
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
            address=address, md_path=target, mtime=0.0,
            preview=preview_create(address.md_rel_path, new_text),
        )

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        mtime = write_md(target, fm, body, expected_mtime=None, create_only=True)
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [target],
                op="create_topic",
                target=address.md_rel_path,
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="create_topic", target=address.md_rel_path,
        mtime_after=mtime, git_sha=sha, reindexed=reindexed,
        extra={"refs": refs_list, "tags": tags_list},
    )

    return WriteResult(
        address=address, md_path=target, mtime=mtime,
        git_sha=sha, reindexed=reindexed,
    )


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
    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    if address.node_type != "topic":
        raise RuleViolation(
            f"topic.update() on a {address.node_type}; wrong module."
        )
    validate_topic_slug(address.key)

    md_path = address.md_abspath(ctx.kb_root)
    existing_fm, existing_body, _ = read_md(md_path)

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

    # No-op detection: see thought.update() for rationale.
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
        mtime = write_md(md_path, new_fm, new_body, expected_mtime=expected_mtime)
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="update_topic",
                target=address.md_rel_path,
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="update_topic", target=address.md_rel_path,
        mtime_before=expected_mtime, mtime_after=mtime,
        git_sha=sha, reindexed=reindexed,
    )

    return WriteResult(
        address=address, md_path=md_path, mtime=mtime,
        git_sha=sha, reindexed=reindexed,
    )
