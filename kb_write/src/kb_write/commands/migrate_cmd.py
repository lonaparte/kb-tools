"""`kb-write migrate-legacy-chapters / migrate-slugs` — one-shot
migrations for historical data shapes."""
from __future__ import annotations


# ---------- migrate-legacy-chapters ----------
def register_legacy_chapters(sub) -> None:
    p = sub.add_parser(
        "migrate-legacy-chapters",
        help=(
            "One-shot migration of v25-style longform chapters "
            "from thoughts/<date>-<KEY>-ch<NN>-*.md to the v26 "
            "canonical location papers/<KEY>-chNN.md. Idempotent: "
            "re-running skips chapters already migrated. Preserves "
            "body content verbatim — no LLM call. Use `--dry-run` "
            "to preview the move plan."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "List the migration plan without touching any files. "
            "Disjoint from the global --dry-run flag (which would "
            "still run audit/git — this flag skips ALL filesystem "
            "writes)."
        ),
    )
    p.set_defaults(func=_cmd_migrate_legacy_chapters)


def _cmd_migrate_legacy_chapters(args, ctx):
    from ..ops.migrate_chapters import (
        migrate_legacy_chapters, format_report,
    )
    report = migrate_legacy_chapters(ctx, dry_run=args.dry_run)
    print(format_report(report))
    # Non-zero exit when there were errors or unresolved collisions
    # so cron wrappers can alarm. Actual skipped-already-done is a
    # normal outcome (idempotent re-run) → rc=0.
    if report.errors or report.skipped_collision:
        return 1
    return 0


# ---------- migrate-slugs ----------
def register_slugs(sub) -> None:
    p = sub.add_parser(
        "migrate-slugs",
        help=(
            "Rename thought/topic mds whose slugs violate the "
            "current lowercase-kebab format (pre-v24 uppercase "
            "Zotero keys, disallowed chars, double-hyphens, etc). "
            "Idempotent: already-canonical files are skipped. "
            "Atomic rename + audit.log + single batch git commit. "
            "Use `--dry-run` to preview."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Print the rename plan without touching any files. "
            "Disjoint from the global --dry-run flag."
        ),
    )
    p.set_defaults(func=_cmd_migrate_slugs)


def _cmd_migrate_slugs(args, ctx):
    from ..ops.migrate_slugs import migrate_slugs, format_report
    report = migrate_slugs(ctx, dry_run=args.dry_run)
    print(format_report(report))
    if report.errors or report.skipped_collision:
        return 1
    return 0
