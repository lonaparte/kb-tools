"""`kb-importer check-orphans` and `kb-importer unarchive`.

Both are lightweight housekeeping commands.

## check-orphans

Finds two kinds of orphans:

- **Orphan md files**: `papers/{k}.md` or
  `topics/standalone-note/{k}.md` (v26; was `zotero-notes/{k}.md`
  in v25) whose paper/note was deleted from Zotero. These are
  detected by comparing the md filenames against Zotero's top-level
  item list.

- **Orphan attachment dirs**: subdirectories under `storage/` or
  `storage/_archived/` whose attachment key is not referenced by any
  imported paper md. We detect these by reading every paper md's
  `zotero_attachment_keys` frontmatter field and seeing what's
  referenced.

  Note: a PDF sitting in `storage/XY7ZK3A2/` is NOT orphan just
  because `XY7ZK3A2` isn't in any imported paper md — its paper might
  simply not have been imported yet. We use Zotero's full attachment
  inventory as the ground truth for "is this attachment still alive in
  Zotero at all?" — but gathering that would require a children()
  call per paper (expensive in web mode). For Phase 1 we just report
  "attachment dir not referenced by any imported paper md" as a hint
  without calling it a definitive orphan.

## unarchive

Takes paper keys (not attachment keys). For each paper, reads its md's
`zotero_attachment_keys` list and moves those attachment dirs from
`_archived/` back to `storage/`. Leaves the md file alone — caller
decides whether to re-import.
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..md_builder import paper_md_path
from ..md_io import read_md
from ..state import (
    imported_note_keys,
    imported_paper_keys,
    scan_attachments,
    unarchive_attachments,
)
from ..zotero_reader import ZoteroReader

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# check-orphans
# ---------------------------------------------------------------------

def add_orphans_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "check-orphans",
        help="Find KB md files and storage dirs with no Zotero counterpart.",
    )
    p.set_defaults(func=run_orphans)


def detect_orphans(
    cfg: Config,
    *,
    include_unarchived: bool = False,
) -> dict:
    """Run the full orphan scan. Shared by CLI `check-orphans` and
    the `kb_report` orphans section.

    Returns a dict with four lists of string keys:
      - orphan_papers          — paper_keys in KB but deleted from Zotero
      - orphan_notes           — note_keys in KB but deleted from Zotero
      - unreferenced_archived  — storage/_archived/ dirs not in any md
      - unreferenced_unarchived — storage/ dirs not in any md (usually
                                  just "not imported yet", so omitted
                                  by default; pass include_unarchived
                                  for completeness)

    Connects to Zotero (mode determined by cfg). Raises whatever
    ZoteroReader raises on connection failure — callers decide
    whether that's fatal or degrade-to-partial.
    """
    reader = ZoteroReader(cfg)
    zotero_paper_keys = reader.list_paper_keys()
    zotero_note_keys = reader.list_standalone_note_keys()

    kb_paper_keys = imported_paper_keys(cfg)
    orphan_papers = kb_paper_keys - zotero_paper_keys

    kb_note_keys = imported_note_keys(cfg)
    orphan_notes = kb_note_keys - zotero_note_keys

    # Attachment-level: referenced_att_keys comes from every imported
    # paper md's frontmatter zotero_attachment_keys list.
    referenced_att_keys: set[str] = set()
    for pk in kb_paper_keys:
        md_path = paper_md_path(cfg.kb_root, pk)
        try:
            post = read_md(md_path)
            att_keys = post.metadata.get("zotero_attachment_keys", []) or []
            referenced_att_keys.update(att_keys)
        except Exception:
            log.warning("couldn't read %s; skipping in orphan scan", md_path)
            continue

    scan = scan_attachments(cfg)
    unreferenced_archived = scan.archived - referenced_att_keys
    unreferenced_unarchived = scan.unarchived - referenced_att_keys

    return {
        "orphan_papers":           sorted(orphan_papers),
        "orphan_notes":            sorted(orphan_notes),
        "unreferenced_archived":   sorted(unreferenced_archived),
        "unreferenced_unarchived": (
            sorted(unreferenced_unarchived) if include_unarchived else []
        ),
    }


def run_orphans(args: argparse.Namespace, cfg: Config) -> int:
    try:
        result = detect_orphans(
            cfg, include_unarchived=getattr(args, "verbose", False),
        )
    except Exception as e:
        print(f"Error: could not connect to Zotero ({cfg.zotero_source_mode} mode): {e}",
              file=sys.stderr)
        return 2

    orphan_papers           = result["orphan_papers"]
    orphan_notes            = result["orphan_notes"]
    unreferenced_archived   = result["unreferenced_archived"]
    unreferenced_unarchived = result["unreferenced_unarchived"]

    any_findings = False

    if orphan_papers:
        any_findings = True
        print(f"Orphan paper md files ({len(orphan_papers)}):")
        for k in orphan_papers:
            print(f"  papers/{k}.md")
    if orphan_notes:
        any_findings = True
        print(f"Orphan note md files ({len(orphan_notes)}):")
        for k in orphan_notes:
            print(f"  topics/standalone-note/{k}.md")
    if unreferenced_archived:
        any_findings = True
        print(
            f"Archived attachment dirs not referenced by any imported paper "
            f"md ({len(unreferenced_archived)}):"
        )
        for k in unreferenced_archived:
            print(f"  storage/_archived/{k}/")
        print(
            "  (These may be archive remnants from deleted papers. Safe to "
            "delete, but check first.)"
        )
    if unreferenced_unarchived:
        any_findings = True
        print(
            f"Unarchived attachment dirs not referenced by any imported "
            f"paper md ({len(unreferenced_unarchived)}):"
        )
        for k in unreferenced_unarchived:
            print(f"  storage/{k}/")
        print(
            "  (These are most likely from papers that haven't been "
            "imported yet, not orphans.)"
        )

    if not any_findings:
        print("No orphans found.")

    return 0


# ---------------------------------------------------------------------
# unarchive
# ---------------------------------------------------------------------

def add_unarchive_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "unarchive",
        help="Move a paper's PDFs from storage/_archived/ back to storage/.",
    )
    p.add_argument(
        "paper_keys", nargs="+",
        help=(
            "Paper item keys to unarchive. Their attachment keys are read "
            "from the paper md's frontmatter. The md itself is not touched."
        ),
    )
    p.set_defaults(func=run_unarchive)


def run_unarchive(args: argparse.Namespace, cfg: Config) -> int:
    dry_run = getattr(args, "dry_run", False)
    total_papers_ok = 0
    total_papers_failed = 0

    for paper_key in args.paper_keys:
        md_path = paper_md_path(cfg.kb_root, paper_key)
        if not md_path.exists():
            print(f"✗ {paper_key}: no md at {md_path}", file=sys.stderr)
            total_papers_failed += 1
            continue

        try:
            post = read_md(md_path)
            att_keys = post.metadata.get("zotero_attachment_keys", []) or []
        except Exception as e:
            print(f"✗ {paper_key}: couldn't read md ({e})", file=sys.stderr)
            total_papers_failed += 1
            continue

        if not att_keys:
            print(f"·  {paper_key}: no attachments to unarchive")
            total_papers_ok += 1
            continue

        if dry_run:
            print(f"(dry-run) would unarchive {paper_key}: {att_keys}")
            total_papers_ok += 1
            continue

        result = unarchive_attachments(cfg, att_keys)
        if result.moved:
            print(f"✓ {paper_key}: unarchived {len(result.moved)} dir(s) "
                  f"({', '.join(result.moved)})")
        if result.already_there:
            print(f"  {paper_key}: {len(result.already_there)} already in storage/")
        if result.not_found:
            print(f"  {paper_key}: {len(result.not_found)} not found anywhere "
                  f"({', '.join(result.not_found)})")
        for ak, reason in result.errors:
            print(f"  ✗ {paper_key}: {ak}: {reason}", file=sys.stderr)

        if result.errors:
            total_papers_failed += 1
        else:
            total_papers_ok += 1

    print(f"Done: {total_papers_ok} papers processed, {total_papers_failed} failed.")
    print(
        "Note: md files were NOT deleted. Run `import papers {paper_keys}` "
        "to re-import, or `rm papers/{key}.md` first if you want fresh ones."
    )
    return 0 if total_papers_failed == 0 else 1
