"""Key resolution for `kb-importer import`.

Extracted from import_cmd.py in v0.28.0. Three functions:
  - _check_keys_not_attachments: flag & drop user keys that are
    actually attachment keys of already-imported papers.
  - _resolve_paper_keys: merge --all-pending, filters, and explicit
    keys into the final paper key set.
  - _resolve_note_keys: same for notes (no --all-pending variant).
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..zotero_reader import ZoteroReader

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Key resolution
# ----------------------------------------------------------------------

def _check_keys_not_attachments(cfg: Config, user_keys: set[str]) -> set[str]:
    """Warn on (and filter out) user-supplied keys that are actually
    attachment keys of already-imported papers.

    This catches the common mistake of passing a `storage/` subdir
    name (attachment key) where a paper key is expected. We scan
    already-imported paper mds' `zotero_attachment_keys` frontmatter
    to build a reverse map, then check each user-supplied key.

    Keys recognized as attachments are skipped with a warning pointing
    to the parent paper. Unknown keys pass through silently — they
    might be paper keys we haven't imported yet.

    Only runs when the user passes explicit keys (not --all-pending),
    so it's O(N mds) but bounded.
    """
    # Local imports to keep the module-level imports clean.
    from ..md_io import read_md
    from ..md_builder import paper_md_path

    imported = {
        p.stem
        for p in cfg.papers_dir.glob("*.md")
        if not p.name.startswith(".")
    } if cfg.papers_dir.exists() else set()

    att_to_paper: dict[str, str] = {}
    for pk in imported:
        try:
            post = read_md(paper_md_path(cfg.kb_root, pk))
        except Exception:
            continue
        atts = post.metadata.get("zotero_attachment_keys", []) or []
        for ak in atts:
            if isinstance(ak, str):
                att_to_paper[ak] = pk

    safe_keys: set[str] = set()
    for key in user_keys:
        parent = att_to_paper.get(key)
        if parent and parent != key:
            print(
                f"⚠  {key!r} looks like an attachment key "
                f"(belongs to paper {parent!r}). Skipping. "
                f"If you want to re-import that paper, use {parent!r}.",
                file=sys.stderr,
            )
            continue
        safe_keys.add(key)
    return safe_keys


def _resolve_paper_keys(
    args: argparse.Namespace, cfg: Config, reader: ZoteroReader
) -> set[str]:
    # --only-key is a comma-separated form of positional keys; merge
    # them so both drive paper selection. Previously --only-key only
    # affected the fulltext pass, which was surprising given its name.
    user_keys: list[str] = list(args.keys or [])
    if args.only_key:
        user_keys.extend(
            k.strip() for k in args.only_key.split(",") if k.strip()
        )
    if user_keys:
        # Sanity check: did any of the given keys look like attachment
        # keys we've already seen in another paper's md? This catches
        # the common mistake of passing a storage/ subdir name (which
        # is an attachment key) where a paper key is expected.
        return _check_keys_not_attachments(cfg, set(user_keys))

    # Compute `imported` (purely local file scan, cheap).
    imported = {
        p.stem
        for p in cfg.papers_dir.glob("*.md")
        if not p.name.startswith(".")
    } if cfg.papers_dir.exists() else set()

    # --all-unprocessed: entirely local — scan md frontmatter for
    # fulltext_processed != true among already-imported papers. No
    # Zotero calls. This matters: real-machine measurement shows
    # reader.list_paper_keys() takes ~12 min on a 1150-paper web-mode
    # library, so a fulltext-only backfill that happens to hit that
    # code path would waste 12 min for nothing. Gated on flags so it
    # only runs when actually needed.
    unprocessed: set[str] = set()
    if args.all_unprocessed:
        from ..fulltext_writeback import is_fulltext_processed
        from ..md_builder import paper_md_path
        for key in imported:
            md = paper_md_path(cfg.kb_root, key)
            if not is_fulltext_processed(md):
                unprocessed.add(key)

    if not (args.collection or args.tag or args.year or args.all_pending
            or args.all_unprocessed):
        # No filter and no explicit keys — require --all-pending or
        # --all-unprocessed to be explicit about batch intent.
        print("Error: specify keys, a filter (--collection/--tag/--year), "
              "--all-pending, or --all-unprocessed.", file=sys.stderr)
        return set()

    # "Pending" = in Zotero but no md yet. Only compute when we
    # actually need it — fetching the Zotero side is the expensive
    # part of this whole function.
    needs_pending = (
        args.all_pending
        or (not args.all_unprocessed and (
            args.collection or args.tag or args.year
        ))
    )
    pending: set[str] = set()
    if needs_pending:
        all_keys = reader.list_paper_keys()
        pending = all_keys - imported

    # Assemble the base set.
    base_set: set[str]
    if args.all_pending and args.all_unprocessed:
        base_set = pending | unprocessed
    elif args.all_pending:
        base_set = pending
    elif args.all_unprocessed:
        base_set = unprocessed
    else:
        # A filter without --all-pending / --all-unprocessed still
        # operates on pending (backwards-compatible with pre-v22
        # behaviour).
        base_set = pending

    if not (args.year or args.collection or args.tag):
        return base_set

    # Filter by year / collection / tag. Requires fetching each item
    # which is slow for large libraries; OK for reasonable batch sizes.
    result: set[str] = set()
    for key in base_set:
        try:
            item = reader.get_paper(key)
        except Exception:
            continue
        if args.year and item.year != args.year:
            continue
        if args.collection and args.collection not in item.collections:
            continue
        if args.tag and args.tag not in item.tags:
            continue
        result.add(key)
    return result


def _resolve_note_keys(
    args: argparse.Namespace, cfg: Config, reader: ZoteroReader
) -> set[str]:
    user_keys: list[str] = list(args.keys or [])
    if args.only_key:
        user_keys.extend(
            k.strip() for k in args.only_key.split(",") if k.strip()
        )
    if user_keys:
        return set(user_keys)

    all_keys = reader.list_standalone_note_keys()
    imported = imported_note_keys(cfg)
    pending = all_keys - imported

    if not (args.tag or args.all_pending):
        print("Error: specify keys, --tag, or --all-pending.", file=sys.stderr)
        return set()

    if args.all_pending:
        return pending

    result: set[str] = set()
    for key in pending:
        try:
            item = reader.get_standalone_note(key)
        except Exception:
            continue
        if args.tag and args.tag not in item.tags:
            continue
        result.add(key)
    return result


