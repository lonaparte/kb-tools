"""`kb-importer sync` — re-import items whose Zotero version has advanced.

For each already-imported item:
  - Read the existing md's `zotero_version`.
  - If Zotero's current version is higher, re-import (preserving AI zone).
  - Otherwise, skip.

## Resetting zotero_* version fields to 0 (operator Q&A)

Q: "Can I safely set `zotero_max_child_version: 0` (or `zotero_version`,
   `zotero_max_attachment_version`, etc.) in paper mds back to 0?"

A: **Yes, safely.** The comparison in `_why_update_paper` is strict
   `remote > local`. When you reset the local to 0, any remote value
   greater than 0 (every real item has one) will flag the paper for
   re-import. The re-import is idempotent at the md level:
   - frontmatter is rewritten with the fresh remote values
   - body is re-built from the item
   - the "preserved" sections (AI zone + any content between custom
     markers, see `md_io.extract_preserved`) are carried over
     verbatim, so manually-added agent notes survive.

   Consequences are purely "more work on the next sync": every
   paper re-imports once, then subsequent syncs are no-ops again.
   Useful for recovering from the pre-0.29 _fetch_children swallow
   bug which could have left some mds with corrupted version
   fields — a bulk reset forces a clean re-import.

   One-liner to reset all papers:

       python3 -c "
       import pathlib, re
       for p in pathlib.Path('papers').glob('*.md'):
           text = p.read_text()
           for field in ('zotero_version', 'zotero_max_child_version',
                          'zotero_max_attachment_version'):
               text = re.sub(rf'^{field}:.*$', f'{field}: 0',
                             text, flags=re.MULTILINE)
           p.write_text(text)
       "

   Then run `kb-importer sync papers` to re-import everything.
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..md_builder import note_md_path, paper_md_path
from ..md_io import read_md
from ..state import imported_note_keys, imported_paper_keys
from ..zotero_reader import ZoteroReader
from .import_cmd import _process_note, _process_paper

log = logging.getLogger(__name__)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "sync",
        help="Re-import items whose Zotero version has changed.",
    )
    p.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=["papers", "notes", "all"],
        help="What to sync (default: all).",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    try:
        reader = ZoteroReader(cfg)
    except Exception as e:
        print(f"Error: could not connect to Zotero local API: {e}")
        return 2

    dry_run = getattr(args, "dry_run", False)
    total_updated = 0
    total_failed = 0

    if args.target in ("papers", "all"):
        updated, failed = _sync_papers(cfg, reader, dry_run=dry_run)
        total_updated += updated
        total_failed += failed

    if args.target in ("notes", "all"):
        updated, failed = _sync_notes(cfg, reader, dry_run=dry_run)
        total_updated += updated
        total_failed += failed

    print(f"Sync done: {total_updated} updated, {total_failed} failed.")
    return 0 if total_failed == 0 else 1


def _sync_papers(cfg: Config, reader: ZoteroReader, *, dry_run: bool) -> tuple[int, int]:
    # Source of truth for "which papers are imported": the md files in
    # papers/. Not the storage/ dir, which is attachment-keyed.
    imported = imported_paper_keys(cfg)
    updated, failed = 0, 0

    # For each imported paper we check five signals (Zotero tracks
    # paper, child notes, and attachments as independent items, each
    # with its own version; edits to children don't bump the paper):
    #   1. Paper's own version
    #   2. Max version across child notes
    #   3. Child note count (catches note deletions)
    #   4. Max version across attachments
    #   5. Attachment count (catches attachment deletions / additions)
    # Any one changing → regenerate.
    for key in sorted(imported):
        md_path = paper_md_path(cfg.kb_root, key)
        if not md_path.exists():
            # imported_paper_keys said it exists; race condition or bug.
            log.warning("skip %s: md vanished at %s", key, md_path)
            continue

        try:
            local = _read_local_paper_state(md_path)
            item = reader.get_paper(key)

            remote_max_child_v = max((n.version for n in item.notes), default=0)
            remote_child_count = len(item.notes)
            remote_max_att_v = max((a.version for a in item.attachments), default=0)
            remote_att_count = len(item.attachments)

            reason = _why_update_paper(
                local, item,
                remote_max_child_v, remote_child_count,
                remote_max_att_v, remote_att_count,
            )
            if reason is None:
                continue

            log.info("Updating paper %s: %s", key, reason)
            _process_paper(cfg, reader, key, dry_run=dry_run)
            updated += 1
            print(f"✓ (paper) {key}  ({reason})")
        except Exception as e:
            failed += 1
            log.exception("sync failed for paper %s", key)
            print(f"✗ (paper) {key}  {e}", file=sys.stderr)

    return updated, failed


def _why_update_paper(
    local: dict,
    item,
    remote_max_child_v: int,
    remote_child_count: int,
    remote_max_att_v: int,
    remote_att_count: int,
) -> str | None:
    """Return a short human-readable reason to update, or None if up-to-date."""
    if item.version > local["zotero_version"]:
        return f"paper v{local['zotero_version']} → v{item.version}"
    if remote_max_child_v > local["zotero_max_child_version"]:
        return (
            f"child note edited "
            f"(max_child_v {local['zotero_max_child_version']} → {remote_max_child_v})"
        )
    if remote_child_count != local["zotero_child_note_count"]:
        return (
            f"child note count "
            f"{local['zotero_child_note_count']} → {remote_child_count}"
        )
    if remote_max_att_v > local["zotero_max_attachment_version"]:
        return (
            f"attachment edited "
            f"(max_att_v {local['zotero_max_attachment_version']} → {remote_max_att_v})"
        )
    if remote_att_count != local["zotero_attachment_count"]:
        return (
            f"attachment count "
            f"{local['zotero_attachment_count']} → {remote_att_count}"
        )
    return None


def _sync_notes(cfg: Config, reader: ZoteroReader, *, dry_run: bool) -> tuple[int, int]:
    imported = imported_note_keys(cfg)
    updated, failed = 0, 0

    for key in sorted(imported):
        md_path = note_md_path(cfg.kb_root, key)
        if not md_path.exists():
            continue

        try:
            local_version = _read_local_version(md_path)
            item = reader.get_standalone_note(key)
            if item.version <= local_version:
                continue
            log.info(
                "Updating note %s: local=%d, remote=%d",
                key, local_version, item.version,
            )
            _process_note(cfg, reader, key, dry_run=dry_run)
            updated += 1
            print(f"✓ (note) {key}  v{local_version} → v{item.version}")
        except Exception as e:
            failed += 1
            log.exception("sync failed for note %s", key)
            print(f"✗ (note) {key}  {e}", file=sys.stderr)

    return updated, failed


def _read_local_paper_state(md_path) -> dict:
    """Read version-tracking fields from a paper md.

    Returns a dict with: zotero_version, zotero_max_child_version,
    zotero_child_note_count, zotero_max_attachment_version,
    zotero_attachment_count. Missing fields default to 0 so that any
    positive remote value triggers an update (safe default — better to
    update spuriously than to miss a change).
    """
    keys = (
        "zotero_version",
        "zotero_max_child_version",
        "zotero_child_note_count",
        "zotero_max_attachment_version",
        "zotero_attachment_count",
    )
    try:
        post = read_md(md_path)
        return {k: int(post.metadata.get(k, 0) or 0) for k in keys}
    except Exception:
        return {k: 0 for k in keys}


def _read_local_version(md_path) -> int:
    """Read zotero_version from a md file. Returns 0 if missing/malformed.

    Kept for the standalone-note sync path, which doesn't need the
    extended state dict.
    """
    try:
        post = read_md(md_path)
        return int(post.metadata.get("zotero_version", 0) or 0)
    except Exception:
        return 0
