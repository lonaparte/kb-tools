"""`kb-importer status` — show overall import progress.

Reports progress at two levels, because Zotero stores things at two
levels:

- **Papers** (top-level items): "imported" = has a paper md in
  `papers/`. This is the answer for "what % of my library have I
  imported?".

- **Attachment PDFs**: "archived" = under `storage/_archived/` (moved
  there after a successful paper import), "unarchived" = still in
  `storage/`. A single paper can have multiple attachments and each is
  tracked independently. This is useful for spotting orphan storage
  dirs (PDFs whose Zotero items are gone).
"""
from __future__ import annotations

import argparse
import logging

from ..config import Config
from ..state import (
    imported_note_keys,
    imported_paper_keys,
    scan_attachments,
)
from ..zotero_reader import ZoteroReader

log = logging.getLogger(__name__)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("status", help="Show overall import progress.")
    p.add_argument(
        "--quick", action="store_true",
        help=(
            "Skip the full Zotero library listing (which can take 60+ "
            "seconds in web mode). Only does a 1-item ping to verify "
            "connectivity, then reports local filesystem state."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    print(f"Zotero source: {cfg.zotero_source_mode}")
    print(f"Zotero storage: {cfg.zotero_storage_dir}")
    print(f"KB root:        {cfg.kb_root}")
    print()

    # Local state (always available, no Zotero needed).
    papers_md = imported_paper_keys(cfg)
    notes_md = imported_note_keys(cfg)
    att_scan = scan_attachments(cfg)

    # Cross-reference with Zotero.
    # --quick: only ping, don't enumerate.
    # default: full library listing (for "Total in Zotero" stats).
    all_paper_keys = None
    all_note_keys = None
    zotero_ok = False
    try:
        reader = ZoteroReader(cfg)
        if args.quick:
            reader.ping()
            zotero_ok = True
            print("✓ Zotero API reachable (quick check only; totals skipped).")
            print()
        else:
            all_paper_keys = reader.list_paper_keys()
            all_note_keys = reader.list_standalone_note_keys()
            zotero_ok = True
    except Exception as e:
        log.warning("Could not connect to Zotero (%s mode): %s",
                    cfg.zotero_source_mode, e)
        if cfg.zotero_source_mode == "live":
            print("⚠  Zotero local API unavailable — showing filesystem state only.")
            print("   (Start Zotero and enable local API to see total counts.)")
        else:
            print(f"⚠  Zotero web API unavailable: {e}")
            print("   Showing filesystem state only.")
        print()

    # -- Papers --
    print("Papers:")
    if zotero_ok and all_paper_keys is not None:
        pending = all_paper_keys - papers_md
        orphan_md = papers_md - all_paper_keys
        print(f"  Total in Zotero:  {len(all_paper_keys)}")
        print(f"  Imported (md):    {len(papers_md & all_paper_keys)}")
        print(f"  Pending:          {len(pending)}")
        if orphan_md:
            print(
                f"  Orphan md files:  {len(orphan_md)} "
                f"(in papers/ but not in Zotero — run `check-orphans`)"
            )
    else:
        print(f"  Imported md files: {len(papers_md)}")
    print()

    # -- Standalone notes --
    print("Standalone notes:")
    if zotero_ok and all_note_keys is not None:
        pending_notes = all_note_keys - notes_md
        actually_imported = all_note_keys & notes_md
        orphan_md = notes_md - all_note_keys
        print(f"  Total in Zotero:  {len(all_note_keys)}")
        print(f"  Imported:         {len(actually_imported)}")
        print(f"  Pending:          {len(pending_notes)}")
        if orphan_md:
            print(
                f"  Orphan md files:  {len(orphan_md)} "
                f"(in topics/standalone-note/ but not in Zotero)"
            )
    else:
        print(f"  Imported md files: {len(notes_md)}")
    print()

    # -- Attachments (PDF-level) --
    # This is separate from paper progress because a paper may have
    # 0, 1, or N PDF attachments. These counts are over ALL attachments
    # (PDFs + non-PDFs), since scan_attachments doesn't distinguish.
    print("Attachment storage dirs:")
    print(f"  Unarchived: {len(att_scan.unarchived)}")
    print(f"  Archived:   {len(att_scan.archived)}")
    print()
    print("  (An archived attachment dir means its paper was successfully")
    print("   imported at some point. Note that one paper can produce")
    print("   multiple archived attachment dirs — main PDF + supplements.)")

    return 0
