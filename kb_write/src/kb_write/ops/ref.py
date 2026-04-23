"""Agent-writable kb_refs operations: add / remove a reference entry.

Structurally identical to tag.py except the validated field and the
value-format check (kb_refs entries must pass validate_kb_ref_entry).
"""
from __future__ import annotations

from ..atomic import write_lock
from ..config import WriteContext
from ..frontmatter import read_md, write_md, merge_kb_fields, remove_from_kb_list
from ..git import auto_commit
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import RuleViolation, validate_kb_ref_entry
from .thought import WriteResult, _nullcontext, _record_audit


def add(
    ctx: WriteContext,
    target: str | NodeAddress,
    ref: str,
    *,
    expected_mtime: float | None = None,
) -> WriteResult:
    """Add `ref` to the target's kb_refs list. `ref` must pass
    validate_kb_ref_entry (e.g. 'papers/ABCD1234', 'topics/xxx')."""
    ref = (ref or "").strip()
    if not ref:
        raise RuleViolation("ref cannot be empty")
    validate_kb_ref_entry(ref)

    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    md_path = address.md_abspath(ctx.kb_root)

    # read+merge+write inside the lock — same lost-update concern as
    # tag.add. Two concurrent `add_kb_ref` calls to the same paper
    # previously kept only one ref, silently dropping the other.
    if ctx.dry_run:
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = merge_kb_fields(existing_fm, {"kb_refs": [ref]})
        if new_fm.get("kb_refs") == existing_fm.get("kb_refs"):
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
            )
        return WriteResult(address=address, md_path=md_path, mtime=0.0)

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = merge_kb_fields(existing_fm, {"kb_refs": [ref]})
        if new_fm.get("kb_refs") == existing_fm.get("kb_refs"):
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
            )
        mtime = write_md(
            md_path, new_fm, body, expected_mtime=expected_mtime,
        )
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="add_kb_ref",
                target=f"{address.md_rel_path}:{ref}",
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="add_kb_ref", target=address.md_rel_path,
        mtime_before=actual_mtime, mtime_after=mtime,
        git_sha=sha, reindexed=reindexed,
        extra={"ref": ref},
    )

    return WriteResult(
        address=address, md_path=md_path, mtime=mtime,
        git_sha=sha, reindexed=reindexed,
    )


def remove(
    ctx: WriteContext,
    target: str | NodeAddress,
    ref: str,
    *,
    expected_mtime: float | None = None,
) -> WriteResult:
    ref = (ref or "").strip()
    if not ref:
        raise RuleViolation("ref cannot be empty")

    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    md_path = address.md_abspath(ctx.kb_root)

    if ctx.dry_run:
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = remove_from_kb_list(existing_fm, "kb_refs", ref)
        if new_fm.get("kb_refs") == existing_fm.get("kb_refs"):
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
            )
        return WriteResult(address=address, md_path=md_path, mtime=0.0)

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = remove_from_kb_list(existing_fm, "kb_refs", ref)
        if new_fm.get("kb_refs") == existing_fm.get("kb_refs"):
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
            )
        mtime = write_md(
            md_path, new_fm, body, expected_mtime=expected_mtime,
        )
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="remove_kb_ref",
                target=f"{address.md_rel_path}:{ref}",
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="remove_kb_ref",
        target=f"{address.md_rel_path}:{ref}",
        mtime_before=actual_mtime, mtime_after=mtime,
        git_sha=sha, reindexed=reindexed,
    )

    return WriteResult(
        address=address, md_path=md_path, mtime=mtime,
        git_sha=sha, reindexed=reindexed,
    )
