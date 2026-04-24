"""Agent-writable tag operations: add / remove a tag on any md.

Works on all four node types (paper, note, topic, thought). Target
is identified by a path like "papers/ABCD1234" or a NodeAddress.

tags are strings; we don't enforce a schema. Merge-append-dedupe on
add, set-remove on remove. Both ops touch only the `kb_tags`
frontmatter field — never zotero_tags, never body, never AI zone.
"""
from __future__ import annotations

from ..atomic import write_lock_paper
from ..config import WriteContext
from ..frontmatter import read_md, write_md, merge_kb_fields, remove_from_kb_list
from ..git import auto_commit
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import RuleViolation
from .thought import WriteResult, _nullcontext, _record_audit


def add(
    ctx: WriteContext,
    target: str | NodeAddress,
    tag: str,
    *,
    expected_mtime: float | None = None,
) -> WriteResult:
    """Add `tag` to the target's kb_tags list (merge-append-dedupe).

    expected_mtime is optional for tag operations — these are often
    called in rapid succession by an agent without wanting to re-read
    the file each time. If None, we skip the conflict check (which
    opens a tiny race window — acceptable for low-stakes metadata
    edits).
    """
    tag = (tag or "").strip()
    if not tag:
        raise RuleViolation("tag cannot be empty")

    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    md_path = address.md_abspath(ctx.kb_root)

    # CRITICAL: read + merge + write must all happen inside the lock.
    # Previous version read outside the lock, which meant two concurrent
    # `kb-write tag add` calls would each see the pre-change state,
    # each compute a "new fm" missing the other's tag, and the later
    # writer silently clobbered the earlier one. Classic lost-update.
    # Dry-run still reads (for diff purposes), but doesn't need the
    # lock — no write happens.
    if ctx.dry_run:
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = merge_kb_fields(existing_fm, {"kb_tags": [tag]})
        if new_fm.get("kb_tags") == existing_fm.get("kb_tags"):
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
                preview=(
                    f"tag {tag!r} already present on "
                    f"{address.md_rel_path} (current kb_tags: "
                    f"{list(existing_fm.get('kb_tags') or [])!r}); "
                    f"would be a no-op."
                ),
            )
        # v0.28.2: populate preview so _emit_result doesn't fall
        # through to "(no changes — write would be a no-op)". Pre-0.28.2
        # would-add dry-runs lied about outcome. G34 stress-run finding.
        return WriteResult(
            address=address, md_path=md_path, mtime=0.0,
            preview=(
                f"would add kb_tag {tag!r} to {address.md_rel_path}\n"
                f"    before: {list(existing_fm.get('kb_tags') or [])!r}\n"
                f"    after:  {list(new_fm.get('kb_tags') or [])!r}"
            ),
        )

    # v0.28.0: per-paper lock, not KB-root lock. Tag add/remove is
    # single-md scoped; global lock blocks concurrent writes to
    # OTHER papers unnecessarily. Per-paper lock still serialises
    # same-paper RMW so lost-update races are impossible.
    with write_lock_paper(ctx.kb_root, address.key) if ctx.lock else _nullcontext():
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = merge_kb_fields(existing_fm, {"kb_tags": [tag]})
        if new_fm.get("kb_tags") == existing_fm.get("kb_tags"):
            # Tag already present — release lock and return no-op.
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
            )

        # expected_mtime: if caller passed one, validate against actual.
        # For within-lock writes the value is mostly defensive — nothing
        # else can have touched the file while we hold the lock.
        mtime = write_md(
            md_path, new_fm, body,
            expected_mtime=expected_mtime,
        )
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="add_kb_tag",
                target=f"{address.md_rel_path}:{tag}",
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="add_kb_tag", target=address.md_rel_path,
        mtime_before=actual_mtime, mtime_after=mtime,
        git_sha=sha, reindexed=reindexed,
        extra={"tag": tag},
    )

    return WriteResult(
        address=address, md_path=md_path, mtime=mtime,
        git_sha=sha, reindexed=reindexed,
    )


def remove(
    ctx: WriteContext,
    target: str | NodeAddress,
    tag: str,
    *,
    expected_mtime: float | None = None,
) -> WriteResult:
    """Remove `tag` from kb_tags. No-op if absent."""
    tag = (tag or "").strip()
    if not tag:
        raise RuleViolation("tag cannot be empty")

    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    md_path = address.md_abspath(ctx.kb_root)

    # See add() for why read+merge+write must be inside the lock.
    if ctx.dry_run:
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = remove_from_kb_list(existing_fm, "kb_tags", tag)
        if new_fm.get("kb_tags") == existing_fm.get("kb_tags"):
            return WriteResult(
                address=address, md_path=md_path, mtime=actual_mtime,
                git_sha=None, reindexed=False,
                preview=(
                    f"tag {tag!r} not present on "
                    f"{address.md_rel_path}; would be a no-op."
                ),
            )
        return WriteResult(
            address=address, md_path=md_path, mtime=0.0,
            preview=(
                f"would remove kb_tag {tag!r} from {address.md_rel_path}\n"
                f"    before: {list(existing_fm.get('kb_tags') or [])!r}\n"
                f"    after:  {list(new_fm.get('kb_tags') or [])!r}"
            ),
        )

    # v0.28.0: per-paper lock, not KB-root lock. Tag add/remove is
    # single-md scoped; global lock blocks concurrent writes to
    # OTHER papers unnecessarily. Per-paper lock still serialises
    # same-paper RMW so lost-update races are impossible.
    with write_lock_paper(ctx.kb_root, address.key) if ctx.lock else _nullcontext():
        existing_fm, body, actual_mtime = read_md(md_path)
        new_fm = remove_from_kb_list(existing_fm, "kb_tags", tag)
        if new_fm.get("kb_tags") == existing_fm.get("kb_tags"):
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
                op="remove_kb_tag",
                target=f"{address.md_rel_path}:{tag}",
                message_body=ctx.commit_message,
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="remove_kb_tag", target=address.md_rel_path,
        mtime_before=actual_mtime, mtime_after=mtime,
        git_sha=sha, reindexed=reindexed,
        extra={"tag": tag},
    )

    return WriteResult(
        address=address, md_path=md_path, mtime=mtime,
        git_sha=sha, reindexed=reindexed,
    )
