"""`kb-importer list papers|notes` — list items, filterable."""
from __future__ import annotations

import argparse
import logging

from ..config import Config
from ..state import imported_note_keys, imported_paper_keys
from ..zotero_reader import ZoteroReader
from ._shared import _nonnegative_int

log = logging.getLogger(__name__)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("list", help="List papers or notes.")
    p.add_argument(
        "target",
        choices=["papers", "notes"],
        help="What to list.",
    )
    filter_group = p.add_mutually_exclusive_group()
    filter_group.add_argument(
        "--pending", action="store_true",
        help="Only items not yet imported (default).",
    )
    filter_group.add_argument(
        "--imported", action="store_true",
        help="Only items already imported.",
    )
    filter_group.add_argument(
        "--all", action="store_true",
        help="All items regardless of status.",
    )

    p.add_argument("--collection", help="Filter by Zotero collection name.")
    p.add_argument("--tag", help="Filter by Zotero tag.")
    p.add_argument("--year", type=int, help="Filter by publication year.")
    p.add_argument(
        "--with-titles", action="store_true",
        help=(
            "Fetch each paper's title/year from Zotero (slow, requires N "
            "API calls). Default: print keys only — fast, no round-trips. "
            "Auto-enabled if any of --collection/--tag/--year is given."
        ),
    )
    p.add_argument(
        "--limit", type=_nonnegative_int, default=0,
        help=(
            "Max rows to print (0 = no limit). Applied efficiently: "
            "only the first N matches are fetched from Zotero, not all."
        ),
    )

    summary_group = p.add_mutually_exclusive_group()
    summary_group.add_argument(
        "--no-summary", action="store_true",
        help=(
            "(papers only, requires --imported) Only papers whose md "
            "doesn't yet have a fulltext summary. Useful before running "
            "set-summary or import-summaries."
        ),
    )
    summary_group.add_argument(
        "--has-summary", action="store_true",
        help="(papers only, requires --imported) Only papers that do have a summary.",
    )

    p.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    # Default status filter is --pending.
    if not (args.pending or args.imported or args.all):
        args.pending = True

    try:
        reader = ZoteroReader(cfg)
    except Exception as e:
        print(f"Error: could not connect to Zotero local API: {e}")
        print("Is Zotero running with local API enabled?")
        return 2

    if args.target == "papers":
        return _list_papers(args, cfg, reader)
    else:
        return _list_notes(args, cfg, reader)


def _list_papers(args, cfg: Config, reader: ZoteroReader) -> int:
    all_keys = reader.list_paper_keys()
    imported = imported_paper_keys(cfg)

    # Determine status filter. Source of truth for "imported" is the
    # presence of a paper md, NOT any storage dir state.
    if args.imported:
        keys = all_keys & imported
    elif args.all:
        keys = all_keys
    else:  # pending
        keys = all_keys - imported

    # --no-summary / --has-summary: only meaningful against imported
    # papers (pending papers have no md to check). We AND the filter
    # with `imported` regardless of the top-level status filter.
    # Papers whose item_type is ineligible for summary (book, thesis,
    # report, webpage) are excluded from BOTH filters — they don't fit
    # the "done" bucket nor the "needs doing" bucket.
    want_summary = args.has_summary
    want_no_summary = args.no_summary
    if (want_summary or want_no_summary):
        summary_status = _summary_status_for_papers(cfg, keys & imported)
        if want_summary:
            keys = {k for k, s in summary_status.items() if s is True}
        else:
            keys = {k for k, s in summary_status.items() if s is False}

    sorted_keys = sorted(keys)

    # Do we need to fetch each paper's full metadata? Only if filters
    # depend on it, OR if --with-titles is set. Plain status listing
    # with a --limit doesn't need ~1000 Zotero round-trips.
    needs_fetch = bool(
        args.year or args.collection or args.tag or args.with_titles
    )

    if not needs_fetch:
        # Fast path: just print keys. Apply --limit without fetching.
        to_show = sorted_keys[: args.limit or None]
        for key in to_show:
            print(key)
        if args.limit and len(sorted_keys) > args.limit:
            print(f"... and {len(sorted_keys) - args.limit} more "
                  f"(total {len(sorted_keys)}; use --limit 0 for all, "
                  f"or --with-titles to fetch titles).")
        elif not args.limit:
            print(f"\n({len(sorted_keys)} total)")
        return 0

    # Slow path: we need to fetch metadata. Stop as soon as we have
    # enough matching rows — no point fetching the remaining 1100
    # papers if the user asked for --limit 5.
    target = args.limit if args.limit else None
    rows: list[tuple[str, str, str]] = []
    fetched = 0
    for key in sorted_keys:
        if target and len(rows) >= target:
            break
        try:
            item = reader.get_paper(key)
            fetched += 1
        except Exception as e:
            log.debug("skip %s: %s", key, e)
            continue

        if args.year and item.year != args.year:
            continue
        if args.collection and args.collection not in item.collections:
            continue
        if args.tag and args.tag not in item.tags:
            continue

        year_str = str(item.year) if item.year else "????"
        title = (item.title or "").replace("\n", " ")
        if len(title) > 80:
            title = title[:77] + "..."
        rows.append((key, year_str, title))

    for key, year, title in rows:
        print(f"{key}  {year}  {title}")

    if target and len(rows) >= target:
        print(
            f"\nShowing first {len(rows)} of up to {len(sorted_keys)} "
            f"candidate papers (fetched {fetched}). Use --limit 0 for all."
        )
    else:
        print(f"\n({len(rows)} shown, {fetched} fetched from Zotero)")

    return 0


def _summary_status_for_papers(cfg: Config, keys) -> dict[str, bool | None]:
    """Return {paper_key: summary_state} for the given keys.

    summary_state:
    - True  → has fulltext (fulltext_processed=true)
    - False → eligible for summary but hasn't been done yet
    - None  → ineligible for summary (book/thesis/report/webpage/etc.)

    Missing md / unreadable → treated as False (eligible, not done).
    """
    from ..md_builder import paper_md_path
    from ..md_io import read_md
    from .summary_cmd import NO_FULLTEXT_ITEM_TYPES

    result = {}
    for k in keys:
        md_path = paper_md_path(cfg.kb_root, k)
        if not md_path.exists():
            result[k] = False
            continue
        try:
            post = read_md(md_path)
            item_type = post.metadata.get("item_type", "")
            if item_type in NO_FULLTEXT_ITEM_TYPES:
                result[k] = None
            elif post.metadata.get("fulltext_processed"):
                result[k] = True
            else:
                result[k] = False
        except Exception:
            result[k] = False
    return result


def _list_notes(args, cfg: Config, reader: ZoteroReader) -> int:
    all_keys = reader.list_standalone_note_keys()
    imported = imported_note_keys(cfg)

    if args.imported:
        keys = all_keys & imported
    elif args.all:
        keys = all_keys
    else:
        keys = all_keys - imported

    rows: list[tuple[str, str]] = []
    for key in sorted(keys):
        try:
            item = reader.get_standalone_note(key)
        except Exception as e:
            log.debug("skip %s: %s", key, e)
            continue

        if args.tag and args.tag not in item.tags:
            continue

        title = (item.title or "(untitled)").replace("\n", " ")
        if len(title) > 80:
            title = title[:77] + "..."
        rows.append((key, title))

    for key, title in rows[: args.limit or None]:
        print(f"{key}  {title}")

    if args.limit and len(rows) > args.limit:
        print(f"... and {len(rows) - args.limit} more.")

    return 0
