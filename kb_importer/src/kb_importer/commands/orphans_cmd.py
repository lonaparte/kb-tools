"""`kb-importer check-orphans`.

Lightweight housekeeping command. Finds two kinds of orphans:

- **Orphan md files**: `papers/{k}.md` or
  `topics/standalone-note/{k}.md` whose paper/note was deleted from
  Zotero. Detected by comparing the md filenames against Zotero's
  top-level item list.

- **Orphan attachment dirs**: subdirectories under `storage/` whose
  attachment key is not referenced by any imported paper md. We
  detect these by reading every paper md's `zotero_attachment_keys`
  frontmatter field and seeing what's referenced.

  Note: a PDF sitting in `storage/XY7ZK3A2/` is NOT orphan just
  because `XY7ZK3A2` isn't in any imported paper md — its paper
  might simply not have been imported yet. Reported only with
  `--verbose`.

## 0.29.1: `unarchive` subcommand removed

Along with the full removal of the `_archived/` feature in state.py,
the `kb-importer unarchive` subcommand is gone. `storage/` no longer
has a sibling `_archived/` directory that kb-importer manages, so
there's nothing to unarchive.

If your pre-0.29 install still has PDFs under a legacy
`storage/_archived/` from the auto-archive era, you can flatten them
manually with `mv storage/_archived/*/ storage/ && rmdir
storage/_archived`. kb-importer no longer reads or writes under
`_archived/` at all.
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
    include_unreferenced: bool = False,
) -> dict:
    """Run the full orphan scan. Shared by CLI `check-orphans` and
    the `kb_report` orphans section.

    Returns a dict with three lists of string keys:
      - orphan_papers        — paper_keys in KB but deleted from Zotero
      - orphan_notes         — note_keys in KB but deleted from Zotero
      - unreferenced_dirs    — storage/ subdirs not in any imported
                               paper md. Typically "not imported yet",
                               so omitted unless `include_unreferenced`.

    Connects to Zotero (mode determined by cfg). Raises whatever
    ZoteroReader raises on connection failure — callers decide
    whether that's fatal or degrade-to-partial.

    0.29.1: replaced unreferenced_archived / unreferenced_unarchived
    with a single unreferenced_dirs list after _archived/ removal.
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
    unreferenced_dirs = scan.dirs - referenced_att_keys

    return {
        "orphan_papers":     sorted(orphan_papers),
        "orphan_notes":      sorted(orphan_notes),
        "unreferenced_dirs": (
            sorted(unreferenced_dirs) if include_unreferenced else []
        ),
    }


def run_orphans(args: argparse.Namespace, cfg: Config) -> int:
    try:
        result = detect_orphans(
            cfg, include_unreferenced=getattr(args, "verbose", False),
        )
    except Exception as e:
        print(f"Error: could not connect to Zotero ({cfg.zotero_source_mode} mode): {e}",
              file=sys.stderr)
        return 2

    orphan_papers     = result["orphan_papers"]
    orphan_notes      = result["orphan_notes"]
    unreferenced_dirs = result["unreferenced_dirs"]

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
    if unreferenced_dirs:
        any_findings = True
        print(
            f"Attachment dirs not referenced by any imported paper md "
            f"({len(unreferenced_dirs)}):"
        )
        for k in unreferenced_dirs:
            print(f"  storage/{k}/")
        print(
            "  (Likely papers you haven't imported yet — NOT necessarily "
            "orphans. Use `kb-importer list pending` to compare.)"
        )

    if not any_findings:
        print("No orphans found.")

    return 0
