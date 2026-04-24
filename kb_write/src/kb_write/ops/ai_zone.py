"""Append a dated entry to the AI zone of a paper or note md.

v26 replaces the v25 "update (full replace)" semantics with
append-only "add one dated entry at the top of the zone". Rationale:

  - Users rarely want to rewrite the whole AI zone wholesale; they
    want to add their latest thought alongside older ones.
  - Keeping old entries means the md accumulates a visible history
    of how the reader's understanding evolved — the same model
    Zotero uses for its per-paper notes.
  - Each entry is a standalone `### YYYY-MM-DD — <title>` section
    so the kb-mcp indexer can chunk it independently.

Entry shape:

    ### 2026-04-23 — worst-case phase margin revisited

    <body paragraph(s)>

Inserted at the TOP of the zone (newest first). Older entries are
preserved verbatim — append-only.

Free-form non-dated content that exists in the zone before any
`### YYYY-MM-DD` heading is kept UNDER the new entry. A user who
wants to clean up older free-form notes must do it manually.

Rules enforced:

  - Both AI zone markers must exist, appear exactly once each, in
    the right order. If they don't, raise ZoneError — use
    `kb-write doctor --fix` to re-insert missing markers.
  - The new entry format is mandatory: title (one line, no newline)
    + body (any length, may contain Markdown).
  - Content outside the zone is NOT modified. mtime guard applies.
  - Atomic write + git auto-commit + reindex trigger as usual.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ..atomic import atomic_write, write_lock_paper
from ..config import WriteContext
from ..git import auto_commit
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import RuleViolation
from ..zones import find_zone, AI_ZONE_START, AI_ZONE_END
from .thought import WriteResult, _nullcontext, _record_audit


def append(
    ctx: WriteContext,
    target: str | NodeAddress,
    expected_mtime: float,
    *,
    title: str,
    body: str,
    entry_date: date | None = None,
) -> WriteResult:
    """Insert a new dated entry at the top of the AI zone.

    Args:
        ctx: WriteContext with kb_root, git flag, etc.
        target: "papers/ABCD1234" or equivalent. v26 also accepts
                "topics/standalone-note/KEY" (note) and book-chapter
                paths like "papers/BOOKKEY-ch03" (a chapter is still
                a paper).
        expected_mtime: mtime from the read that produced the body.
        title: one-line title; becomes part of the
               `### YYYY-MM-DD — title` heading.
        body:  entry body; may contain markdown.
        entry_date: optional date override (defaults to today).

    Raises:
        RuleViolation: bad target, or title/body empty, or title
                       contains newline.
        ZoneError: markers missing/duplicated/malformed.
        WriteConflictError: mtime mismatch.
        FileNotFoundError: target md doesn't exist.
    """
    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    if address.node_type not in ("paper", "note"):
        raise RuleViolation(
            f"ai_zone.append on {address.node_type!r}; only paper and "
            "note mds have AI zones. Thought and topic files are "
            "fully owned by the agent — edit them with "
            "thought.update / topic.update instead."
        )

    t = (title or "").strip()
    b = (body or "").strip()
    if not t:
        raise RuleViolation("ai_zone.append: title is empty")
    if "\n" in t:
        raise RuleViolation(
            "ai_zone.append: title must be one line (no newline)"
        )
    if not b:
        raise RuleViolation("ai_zone.append: body is empty")

    d = entry_date or date.today()
    date_str = d.isoformat()

    md_path = address.md_abspath(ctx.kb_root)
    original_text = md_path.read_text(encoding="utf-8")
    loc = find_zone(original_text)

    # Build the new entry. Heading format: `### 2026-04-23 — title`.
    # Em-dash (—, U+2014) is the convention.
    new_entry = f"### {date_str} — {t}\n\n{b}\n"

    existing = loc.body.strip("\n")
    if existing:
        new_body = new_entry.rstrip("\n") + "\n\n" + existing + "\n"
    else:
        new_body = new_entry

    # Splice: keep bytes outside the zone verbatim, replace body.
    before = original_text[:loc.start]
    after = original_text[loc.end:]
    new_text = (
        before + AI_ZONE_START + "\n\n" + new_body.strip("\n")
        + "\n\n" + AI_ZONE_END + after
    )

    if ctx.dry_run:
        from ..diff import make_diff
        diff = make_diff(original_text, new_text, path=address.md_rel_path)
        return WriteResult(
            address=address, md_path=md_path, mtime=0.0, diff=diff,
        )

    # v0.28.0: per-paper lock — see tag.py for rationale.
    with write_lock_paper(ctx.kb_root, address.key) if ctx.lock else _nullcontext():
        atomic_write(md_path, new_text, expected_mtime=expected_mtime)
        new_mtime = md_path.stat().st_mtime

        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="append_ai_zone",
                target=address.md_rel_path,
                message_body=ctx.commit_message or f"{date_str}: {t}",
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    _record_audit(
        ctx, op="append_ai_zone", target=address.md_rel_path,
        mtime_before=expected_mtime, mtime_after=new_mtime,
        git_sha=sha, reindexed=reindexed,
    )

    return WriteResult(
        address=address,
        md_path=md_path,
        mtime=new_mtime,
        git_sha=sha,
        reindexed=reindexed,
    )


def read_zone(kb_root: Path, target: str | NodeAddress) -> tuple[str, float]:
    """Return (zone_body, mtime) for the given target md. Used by
    `kb-write ai-zone show` so the caller can read-then-append with
    an mtime guard.

    Raises ZoneError if the zone is malformed / markers missing.
    """
    address = target if isinstance(target, NodeAddress) else parse_target(str(target))
    if address.node_type not in ("paper", "note"):
        raise RuleViolation(
            f"ai_zone.read_zone on {address.node_type!r}; only paper "
            "and note mds have AI zones."
        )
    md_path = address.md_abspath(kb_root)
    text = md_path.read_text(encoding="utf-8")
    loc = find_zone(text)
    return (loc.body.strip("\n"), md_path.stat().st_mtime)
