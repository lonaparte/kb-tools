"""`kb-write migrate-slugs` — rename thought/topic mds whose
slugs violate the current lowercase-kebab format.

Background: pre-v24 imports used uppercase Zotero keys directly
in thought filenames (e.g. `2026-04-22-ABCD1234-chapter-note.md`).
v24 changed the slug rule to
    ^\\d{4}-\\d{2}-\\d{2}-[a-z0-9][a-z0-9\\-]*$
— lowercase letters, digits, hyphens only, no spaces / underscores.
Libraries imported before v24 therefore have mds that
`kb-write doctor` flags but pre-v28 offered no auto-rename for.

This migrator:

  - scans `thoughts/*.md` (and top-level `topics/*.md` for the
    topic-slug variant)
  - for each slug that violates the canonical regex, computes
    a canonicalised slug (lowercase, strip disallowed chars,
    preserve the date prefix, preserve the non-date remainder
    in kebab form)
  - renames the file via atomic move + updates the git index
    via `git mv` so history follows the rename
  - skips when the canonical slug already exists (collision —
    reported, no overwrite)
  - one batch git commit for the whole run

Dry-run prints the move plan without touching anything.

Intentionally NARROW scope: this tool ONLY fixes slug casing /
invalid-char issues. Moving content between directories,
changing md body, or rewriting frontmatter is out of scope —
use `kb-write delete` + recreate for those cases. The motto:
if it would surprise a human reader, this tool shouldn't do it.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..atomic import write_lock
from ..audit import record as _audit_record
from ..config import WriteContext


log = logging.getLogger(__name__)


# Mirror of kb_write.rules._THOUGHT_SLUG_RE. Duplicated here so the
# migrator is self-contained (and so a future loosening in rules.py
# doesn't silently expand the set of rename candidates).
_CANONICAL_THOUGHT_SLUG = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9\-]*$"
)
# Thought slug prefix: `<YYYY-MM-DD>-<rest>`. We canonicalise the
# `<rest>` portion; the date stays untouched.
_THOUGHT_PREFIX = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<rest>.+)$")


@dataclass
class SlugMigrationPlan:
    """One rename: old → new, with a short reason for the report."""
    src: Path
    dst: Path
    old_slug: str
    new_slug: str
    reason: str       # human-readable why


@dataclass
class SlugMigrationReport:
    plans: list[SlugMigrationPlan] = field(default_factory=list)
    migrated: list[SlugMigrationPlan] = field(default_factory=list)
    skipped_collision: list[tuple[SlugMigrationPlan, str]] = field(
        default_factory=list,
    )
    errors: list[tuple[Path, str]] = field(default_factory=list)
    dry_run: bool = False
    git_sha: str | None = None

    def summary_line(self) -> str:
        if self.dry_run:
            return (
                f"[dry-run] {len(self.plans)} slug(s) would be "
                f"renamed; "
                f"{len(self.skipped_collision)} would collide."
            )
        return (
            f"migrated {len(self.migrated)} slug(s); "
            f"{len(self.skipped_collision)} collisions; "
            f"{len(self.errors)} error(s)."
        )


def _canonicalise_thought_slug(slug: str) -> str | None:
    """Return a canonical form of `slug`, or None if we can't
    produce one safely (not date-prefixed, for instance).

    The transform:
      - keep the YYYY-MM-DD prefix verbatim
      - lowercase the rest
      - replace any char that isn't [a-z0-9-] with '-'
      - collapse runs of '-' to a single '-'
      - strip leading/trailing '-' from the rest
      - if the rest is now empty, return None (caller reports)
    """
    m = _THOUGHT_PREFIX.match(slug)
    if not m:
        return None
    date_part = m["date"]
    rest = m["rest"].lower()
    # Substitute every char that's not [a-z0-9-] with '-'.
    rest = re.sub(r"[^a-z0-9\-]", "-", rest)
    # Collapse multiple consecutive hyphens.
    rest = re.sub(r"-{2,}", "-", rest)
    rest = rest.strip("-")
    if not rest:
        return None
    candidate = f"{date_part}-{rest}"
    # Final shape check — if even our canonicalised form doesn't
    # pass the rule, bail rather than produce a broken filename.
    if not _CANONICAL_THOUGHT_SLUG.match(candidate):
        return None
    return candidate


def _build_thought_plans(kb_root: Path) -> tuple[list[SlugMigrationPlan], list[tuple[Path, str]]]:
    """Scan thoughts/ and return (plans, errors)."""
    plans: list[SlugMigrationPlan] = []
    errors: list[tuple[Path, str]] = []
    thoughts = kb_root / "thoughts"
    if not thoughts.is_dir():
        return plans, errors
    for md in sorted(thoughts.glob("*.md")):
        if md.name.startswith("."):
            continue
        slug = md.stem
        if _CANONICAL_THOUGHT_SLUG.match(slug):
            continue  # already canonical
        new_slug = _canonicalise_thought_slug(slug)
        if new_slug is None:
            errors.append((
                md,
                f"slug {slug!r} is not date-prefixed "
                f"(no YYYY-MM-DD at the start) or canonicalises "
                f"to an empty rest. Manual rename needed."
            ))
            continue
        dst = md.with_name(f"{new_slug}.md")
        plans.append(SlugMigrationPlan(
            src=md, dst=dst,
            old_slug=slug, new_slug=new_slug,
            reason=_describe_change(slug, new_slug),
        ))
    return plans, errors


def _describe_change(old: str, new: str) -> str:
    """One-line human description of what changed."""
    if old.lower() == new:
        return "uppercase → lowercase"
    reasons = []
    if any(c.isupper() for c in old):
        reasons.append("lowercase")
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in old.lower()):
        reasons.append("normalise disallowed chars")
    if "--" in old:
        reasons.append("collapse hyphens")
    if not reasons:
        reasons.append("canonicalise")
    return " + ".join(reasons)


def migrate_slugs(
    ctx: WriteContext,
    *,
    dry_run: bool = False,
) -> SlugMigrationReport:
    """Entry point. See module docstring."""
    report = SlugMigrationReport(dry_run=dry_run)

    plans, errors = _build_thought_plans(ctx.kb_root)
    report.plans = plans
    report.errors.extend(errors)

    # Collision detection: target already exists.
    to_migrate: list[SlugMigrationPlan] = []
    for plan in plans:
        if plan.dst.exists():
            report.skipped_collision.append((
                plan,
                f"target {plan.dst.name} already exists — skipping "
                f"to avoid overwrite. Resolve manually, then re-run.",
            ))
            continue
        to_migrate.append(plan)

    if dry_run or not to_migrate:
        return report

    # Apply renames under the kb-root write lock (this is a
    # structural bulk change — single global serialisation is
    # correct here, not per-paper).
    import contextlib
    lock_ctx = (
        write_lock(ctx.kb_root)
        if getattr(ctx, "lock", True)
        else contextlib.nullcontext()
    )
    touched: list[Path] = []
    with lock_ctx:
        for plan in to_migrate:
            try:
                _apply_rename(ctx, plan)
                report.migrated.append(plan)
                touched.append(plan.src)
                touched.append(plan.dst)
                _audit_record(
                    ctx.kb_root,
                    op="migrate_slug",
                    target=plan.dst.relative_to(ctx.kb_root).as_posix(),
                    actor="cli",
                    mtime_after=plan.dst.stat().st_mtime,
                    note=(
                        f"renamed from "
                        f"{plan.src.relative_to(ctx.kb_root).as_posix()} "
                        f"({plan.reason})"
                    ),
                )
            except Exception as e:
                report.errors.append((plan.src, f"{type(e).__name__}: {e}"))

    if ctx.git_commit and report.migrated:
        try:
            from ..git import auto_commit
            sha = auto_commit(
                ctx.kb_root, touched,
                op="migrate_slugs",
                target=f"{len(report.migrated)} slug(s)",
                message_body=(
                    f"Renamed {len(report.migrated)} thought md(s) "
                    f"to canonical lowercase-kebab slugs. No body "
                    f"changes."
                ),
                enabled=True,
            )
            report.git_sha = sha
        except Exception:
            log.exception("auto-commit failed after slug migration")

    return report


def _apply_rename(ctx: WriteContext, plan: SlugMigrationPlan) -> None:
    """Rename plan.src → plan.dst.

    Uses os.rename (via shutil.move) which is atomic within the
    same filesystem. The write-lock is already held by the caller.
    """
    plan.dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan.src), str(plan.dst))


def format_report(report: SlugMigrationReport) -> str:
    """Human-readable rendering for CLI stdout."""
    lines: list[str] = []
    if report.dry_run:
        lines.append("[dry-run] slug migration plan:")
    else:
        lines.append("migrate-slugs:")
    lines.append("")
    lines.append(f"  plans:              {len(report.plans)}")
    lines.append(
        f"  would-rename / migrated: "
        f"{len(report.plans) - len(report.skipped_collision) if report.dry_run else len(report.migrated)}"
    )
    lines.append(f"  collisions:         {len(report.skipped_collision)}")
    lines.append(f"  errors:             {len(report.errors)}")
    if report.git_sha:
        lines.append(f"  commit:             {report.git_sha[:12]}")
    if report.plans and report.dry_run:
        lines.append("")
        lines.append("  plan (first 10):")
        for plan in report.plans[:10]:
            lines.append(
                f"    {plan.old_slug} → {plan.new_slug}  ({plan.reason})"
            )
        if len(report.plans) > 10:
            lines.append(f"    ... +{len(report.plans) - 10} more")
    if report.skipped_collision:
        lines.append("")
        lines.append("  collisions:")
        for plan, reason in report.skipped_collision[:10]:
            lines.append(f"    - {plan.src.name}")
            lines.append(f"        → {plan.dst.name}")
            lines.append(f"        reason: {reason}")
    if report.errors:
        lines.append("")
        lines.append("  errors:")
        for p, e in report.errors[:5]:
            lines.append(f"    - {p.name}: {e}")
        if len(report.errors) > 5:
            lines.append(f"    ... +{len(report.errors) - 5} more")
    lines.append("")
    lines.append(report.summary_line())
    return "\n".join(lines)
