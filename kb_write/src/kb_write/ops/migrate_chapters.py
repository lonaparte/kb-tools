"""`kb-write migrate-legacy-chapters` — move v25-style longform
chapters from `thoughts/<date>-<KEY>-ch<NN>-<slug>.md` to the v26
canonical location `papers/<KEY>-ch<NN>.md`.

Background: pre-v24, the longform pipeline wrote each book /
long-article chapter as a *thought* with a decorated filename and
`kind: thought`. v26's data model treats chapters as first-class
paper mds sharing the parent's `zotero_key`, with filename
`papers/<KEY>-chNN.md` and `kind: paper`. Libraries that were
imported under the old pipeline accumulate chapter mds under
`thoughts/` that `kb-mcp index-status --deep` correctly flags as
deprecated but can't auto-fix.

This migrator handles that one-shot move:

  - match legacy filename pattern `<date>-<KEY>-ch<NN>-<slug>.md`
    under `thoughts/`
  - translate old frontmatter (`kind: thought`, `source_paper`,
    `source_chapter`, `source_type: book_chapter`, `kb_refs`,
    `kb_tags`) → new v26 shape (`kind: paper`, `zotero_key`,
    `item_type: book_chapter`, `chapter_number`, `parent_paper`,
    plus the kb-fulltext wrapper + empty AI zone the v26
    template expects)
  - preserve the body verbatim (no LLM call, no summary
    regeneration — the content is what it is)
  - write the new `papers/<KEY>-chNN.md` via atomic_write,
    then delete the old thought
  - batch into a single git commit (one 182-file commit beats
    182 1-file commits for this bulk migration — kb-write's
    per-op auto-commit would bloat the log otherwise)

Idempotent: if the target `papers/<KEY>-chNN.md` already exists,
the old thought is skipped (counted as "already-migrated"). Safe
to re-run after a partial migration.

`--dry-run` lists the move plan without touching the filesystem.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..atomic import atomic_write, WriteExistsError
from ..audit import record as _audit_record
from ..config import WriteContext


log = logging.getLogger(__name__)


# Legacy filename pattern: `<YYYY-MM-DD>-<KEY>-ch<NN>-<slug>.md`
# where KEY is the parent Zotero key (uppercase+digits), NN is
# 1+ digits (zero-padded normally, but tolerate 1-9), and slug is
# whatever the v24/25 slug generator produced.
#
# Anchored at filename start so we don't accidentally match nested
# `-chNN-` substrings elsewhere in the filename.
_LEGACY_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"-(?P<key>[A-Z0-9]{8})"
    r"-ch(?P<chno>\d+)"
    r"-(?P<slug>.+)"
    r"\.md$"
)

# Optional title suffix the v24/v25 longform pipeline added:
# "<parent title> — Chapter <N>: <chapter title>". We split on
# this to recover the pieces. The em-dash is U+2014.
_TITLE_SPLIT_RE = re.compile(
    r"^(?P<parent>.+?)\s*—\s*Chapter\s+(?P<num>\d+):\s*(?P<chtitle>.+)$"
)


# v26 zone markers — inlined so the migrator doesn't hard-depend
# on kb_importer's md_io constants. If these ever drift between
# the two, migrate-legacy-chapters becomes load-bearing for
# keeping them in sync; a single source in kb_core would be
# better long-term but isn't yet extracted.
_AI_ZONE_START = "<!-- kb-ai-zone-start -->"
_AI_ZONE_END = "<!-- kb-ai-zone-end -->"
_FULLTEXT_START = "<!-- kb-fulltext-start -->"
_FULLTEXT_END = "<!-- kb-fulltext-end -->"


@dataclass
class ChapterPlan:
    """One proposed migration: old → new. Produced during the
    scan-and-plan phase; consumed during write."""
    src: Path                         # thoughts/<...>.md
    dst: Path                         # papers/<KEY>-chNN.md
    parent_key: str                   # e.g. "27HWCL57"
    chapter_number: int
    parent_title: str | None          # extracted from legacy title if possible
    chapter_title: str | None
    legacy_frontmatter: dict          # raw dict from python-frontmatter


@dataclass
class MigrationReport:
    plans: list[ChapterPlan] = field(default_factory=list)
    migrated: list[ChapterPlan] = field(default_factory=list)
    skipped_already_done: list[ChapterPlan] = field(default_factory=list)
    skipped_collision: list[tuple[ChapterPlan, str]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    dry_run: bool = False
    git_sha: str | None = None

    def summary_line(self) -> str:
        if self.dry_run:
            return (
                f"[dry-run] {len(self.plans)} legacy chapter(s) would "
                f"be migrated to papers/; "
                f"{len(self.skipped_already_done)} already present in "
                f"papers/ (would be skipped); "
                f"{len(self.skipped_collision)} would collide."
            )
        return (
            f"migrated {len(self.migrated)} chapter(s) to papers/; "
            f"skipped {len(self.skipped_already_done)} already-present; "
            f"{len(self.skipped_collision)} collisions; "
            f"{len(self.errors)} error(s)."
        )


def migrate_legacy_chapters(
    ctx: WriteContext,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    """Entry point. See module docstring for semantics.

    Never raises; errors are recorded in the report's `errors`
    list so the caller can continue reporting on the remaining
    chapters. Returns a report with the plan counts (and, if not
    dry-run, the realised counts + git sha if auto-commit was
    requested via the WriteContext).
    """
    report = MigrationReport(dry_run=dry_run)
    thoughts_dir = ctx.kb_root / "thoughts"
    papers_dir = ctx.kb_root / "papers"
    if not thoughts_dir.is_dir():
        log.info("no thoughts/ dir — nothing to migrate.")
        return report

    # --- Phase 1: scan + plan ---
    for md in sorted(thoughts_dir.glob("*.md")):
        try:
            plan = _build_plan(md, papers_dir)
        except Exception as e:  # deliberately wide — bad frontmatter etc.
            report.errors.append((md, f"{type(e).__name__}: {e}"))
            continue
        if plan is None:
            continue  # filename doesn't match → not a legacy chapter
        report.plans.append(plan)

    # --- Phase 2: classify vs already-migrated or collision ---
    to_migrate: list[ChapterPlan] = []
    for plan in report.plans:
        if plan.dst.exists():
            # Collision. Distinguish "already migrated" (target has
            # matching zotero_key + chapter_number → same chapter,
            # probably a second run) from "unexpected collision"
            # (target is some other paper at that path).
            collision_reason = _inspect_collision(plan)
            if collision_reason is None:
                report.skipped_already_done.append(plan)
            else:
                report.skipped_collision.append((plan, collision_reason))
            continue
        to_migrate.append(plan)

    # Re-file: skipped_collision + skipped_already_done already
    # populated above; now only to_migrate remains.
    if dry_run:
        # Return without writing; caller formats the plan.
        return report

    # --- Phase 3: write new, delete old, under the write-lock ---
    if not to_migrate:
        return report

    # write_lock() is always-on; when the caller passes
    # ctx.lock=False (e.g. tests that already serialise at a
    # higher level, or --no-lock for debugging) we skip it via a
    # null context manager.
    import contextlib
    from ..atomic import write_lock
    lock_ctx = (
        write_lock(ctx.kb_root)
        if getattr(ctx, "lock", True)
        else contextlib.nullcontext()
    )
    with lock_ctx:
        for plan in to_migrate:
            try:
                _apply_plan(ctx, plan)
                report.migrated.append(plan)
                _audit_record(
                    ctx.kb_root,
                    op="migrate_legacy_chapter",
                    target=plan.dst.relative_to(ctx.kb_root).as_posix(),
                    actor="cli",
                    mtime_after=plan.dst.stat().st_mtime,
                    note=(
                        f"from {plan.src.relative_to(ctx.kb_root).as_posix()}"
                    ),
                )
            except Exception as e:
                report.errors.append((plan.src, f"{type(e).__name__}: {e}"))

    # --- Phase 4: single batch commit if auto-commit enabled ---
    if ctx.git_commit and report.migrated:
        try:
            from ..git import auto_commit
            touched: list[Path] = []
            for p in report.migrated:
                touched.append(p.dst)
                touched.append(p.src)  # deletion is also a tracked change
            sha = auto_commit(
                ctx.kb_root, touched,
                op="migrate_legacy_chapters",
                target=f"{len(report.migrated)} chapter(s)",
                message_body=(
                    f"Moved {len(report.migrated)} legacy chapter md(s) "
                    f"from thoughts/ to papers/<KEY>-chNN.md per the "
                    f"v26 layout. No body changes."
                ),
                enabled=True,
            )
            report.git_sha = sha
        except Exception:
            log.exception("auto-commit failed; migration files landed "
                          "but were not committed")

    # --- Phase 5: trigger reindex so kb-mcp picks up the move ---
    # Only if caller asked; otherwise the next `kb-mcp index` will.
    if ctx.reindex and report.migrated:
        try:
            from ..reindex import trigger_reindex
            trigger_reindex(ctx.kb_root, enabled=True)
        except Exception:
            log.exception("post-migration reindex trigger failed")

    return report


# =====================================================================
# Internals
# =====================================================================

def _build_plan(md: Path, papers_dir: Path) -> ChapterPlan | None:
    """Return a ChapterPlan if `md` looks like a legacy chapter
    thought, else None.

    Identification uses filename pattern + frontmatter signals
    (either `source_type: book_chapter` OR `kind: thought` +
    `source_chapter:` present). We accept the union because the
    v24 and v25 eras used slightly different frontmatter shapes
    and both ended up in real user libraries.
    """
    m = _LEGACY_FILENAME_RE.match(md.name)
    if not m:
        return None
    parent_key = m["key"]
    chapter_number = int(m["chno"])

    import frontmatter
    post = frontmatter.load(str(md))
    fm = post.metadata

    # Filter: must smell like a legacy chapter thought. A normal
    # thought (`kind: thought` with no source_chapter) isn't us.
    kind = fm.get("kind")
    source_chapter = fm.get("source_chapter")
    source_type = fm.get("source_type")
    if kind == "thought" and source_chapter is None and source_type != "book_chapter":
        # Filename matches pattern but frontmatter says "I'm just
        # a regular thought whose title happens to include -chNN-".
        # Safer to skip than rewrite into papers/.
        return None

    # Title split — best-effort, no hard failure if the title
    # doesn't follow the legacy convention.
    title_raw = fm.get("title") or ""
    parent_title: str | None = None
    chapter_title: str | None = None
    tm = _TITLE_SPLIT_RE.match(title_raw.strip())
    if tm:
        parent_title = tm["parent"].strip()
        chapter_title = tm["chtitle"].strip()
    else:
        # Fallback: keep the whole title as chapter_title; no parent
        # extracted. Rare in practice — kept so we don't silently
        # lose a legacy chapter with a quirky title.
        chapter_title = title_raw.strip() or None

    dst = papers_dir / f"{parent_key}-ch{chapter_number:02d}.md"
    return ChapterPlan(
        src=md,
        dst=dst,
        parent_key=parent_key,
        chapter_number=chapter_number,
        parent_title=parent_title,
        chapter_title=chapter_title,
        legacy_frontmatter=dict(fm),
    )


def _inspect_collision(plan: ChapterPlan) -> str | None:
    """If `plan.dst` already exists, decide whether it's:
      - the SAME chapter we'd have written (idempotent re-run) → None
      - something DIFFERENT (real collision; skip + report) → str reason
    """
    import frontmatter
    try:
        existing = frontmatter.load(str(plan.dst))
    except Exception as e:
        return f"unreadable existing target: {e}"
    fm = existing.metadata
    zk = fm.get("zotero_key")
    cn = fm.get("chapter_number")
    if zk == plan.parent_key and cn == plan.chapter_number:
        return None  # same chapter — already migrated
    return (
        f"target exists with zotero_key={zk!r} chapter_number={cn!r}, "
        f"not this chapter (expected zotero_key={plan.parent_key!r} "
        f"chapter_number={plan.chapter_number!r})"
    )


def _apply_plan(ctx: WriteContext, plan: ChapterPlan) -> None:
    """Write the new `papers/<KEY>-chNN.md` and delete the old
    thought. Raises on any failure (caller records in
    report.errors)."""
    plan.dst.parent.mkdir(parents=True, exist_ok=True)

    # Read old body (without frontmatter).
    import frontmatter
    post = frontmatter.load(str(plan.src))
    body = post.content.strip("\n")

    new_text = _render_new_md(plan, body)
    atomic_write(plan.dst, new_text, create_only=True)

    # Only after the new file is safely on disk do we delete the
    # old one. If the delete fails we still have the new chapter
    # in place; the dangling old thought will be caught on the
    # next migrate run (and is_book_chapter check will route it
    # to skipped_already_done, so it's idempotent).
    plan.src.unlink()


def _render_new_md(plan: ChapterPlan, body: str) -> str:
    """Produce the v26 canonical chapter md text.

    Frontmatter: kind=paper, zotero_key, item_type=book_chapter,
    chapter_number, parent_paper, title (reconstructed), kb_refs
    back-reference, fulltext_processed=true so the indexer treats
    the chapter as summarised content (it IS the summary — we're
    just relocating it).

    Body: optional heading, then the preserved body wrapped in
    kb-fulltext markers, then an empty AI zone.
    """
    parent_title = plan.parent_title or plan.parent_key
    chapter_title = plan.chapter_title or f"Chapter {plan.chapter_number}"

    # Compose title. Keep the legacy "— Chapter N: …" suffix so
    # search results / list UIs stay readable.
    new_title = f"{parent_title} — Chapter {plan.chapter_number}: {chapter_title}"

    fm_lines = [
        "---",
        "kind: paper",
        f"zotero_key: {plan.parent_key}",
        "item_type: book_chapter",
        f'title: "{_yaml_escape(new_title)}"',
        f"chapter_number: {plan.chapter_number}",
        f"parent_paper: papers/{plan.parent_key}",
        "fulltext_processed: true",
        "fulltext_source: legacy_migrated",
        f"kb_refs: [papers/{plan.parent_key}]",
        "kb_tags: [longform, chapter]",
    ]
    # Preserve date_added / date_modified if the legacy frontmatter
    # had a longform_generated_at or similar — it's informational
    # and harmless to keep.
    if "longform_generated_at" in plan.legacy_frontmatter:
        fm_lines.append(
            f'longform_generated_at: '
            f'"{plan.legacy_frontmatter["longform_generated_at"]}"'
        )
    fm_lines.append("---")

    body_lines = [
        "",
        f"# {new_title}",
        "",
        _FULLTEXT_START,
        "",
        body,
        "",
        _FULLTEXT_END,
        "",
        "---",
        "",
        _AI_ZONE_START,
        _AI_ZONE_END,
        "",
    ]

    return "\n".join(fm_lines) + "\n" + "\n".join(body_lines)


def _yaml_escape(s: str) -> str:
    """Escape for embedding inside a double-quoted YAML scalar.
    Kept very simple (double-quote + backslash) because chapter
    titles only contain prose, no control chars.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def format_report(report: MigrationReport) -> str:
    """Human-readable rendering for CLI stdout."""
    lines: list[str] = []
    if report.dry_run:
        lines.append("[dry-run] migration plan:")
    else:
        lines.append("migrate-legacy-chapters:")
    lines.append("")
    lines.append(f"  plans:                {len(report.plans)}")
    lines.append(f"  would-migrate / migrated: "
                 f"{len(report.plans) - len(report.skipped_already_done) - len(report.skipped_collision) if report.dry_run else len(report.migrated)}")
    lines.append(f"  already-migrated:     {len(report.skipped_already_done)}")
    lines.append(f"  collisions:           {len(report.skipped_collision)}")
    lines.append(f"  errors:               {len(report.errors)}")
    if report.git_sha:
        lines.append(f"  commit:               {report.git_sha[:12]}")
    if report.skipped_collision:
        lines.append("")
        lines.append("  collisions (details):")
        for plan, reason in report.skipped_collision[:10]:
            lines.append(f"    - {plan.src.name}")
            lines.append(f"        → {plan.dst.name}")
            lines.append(f"        reason: {reason}")
        if len(report.skipped_collision) > 10:
            lines.append(f"    ... +{len(report.skipped_collision) - 10} more")
    if report.errors:
        lines.append("")
        lines.append("  errors (details):")
        for p, e in report.errors[:5]:
            lines.append(f"    - {p.name}: {e}")
        if len(report.errors) > 5:
            lines.append(f"    ... +{len(report.errors) - 5} more")
    lines.append("")
    lines.append(report.summary_line())
    return "\n".join(lines)
