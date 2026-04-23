"""Narrow public API for writing citation data to the kb-mcp
projection DB.

History (v25): kb_citations was previously reaching directly into
kb-mcp's SQLite — hand-writing `DELETE FROM links WHERE origin = ?`,
`INSERT INTO links (...)`, `UPDATE papers SET citation_count = ?` etc.
That made the `links` and `papers` table schemas a de-facto public
contract — any rename / CHECK-constraint tightening / column
addition in kb_mcp would silently break kb_citations at runtime.

This module is the stable boundary: kb_citations calls these
functions, kb_mcp owns the SQL. Both functions are defensive no-ops
when kb_mcp's DB doesn't exist yet (first install) so kb_citations
operations never raise just because indexing hasn't run.

API surface (keep minimal — only what kb_citations actually needs):

- `apply_citation_edges(kb_root, edges)` — atomic full-replace of
  all origin='citation' rows in the `links` table.
- `update_citation_count(kb_root, zotero_key, count, source)` —
  write citation_count + source + timestamp for one paper.
- `list_papers_with_doi(kb_root)` — returns (key, doi) rows for the
  bulk refresh loop. Read-only.
- `count_papers(kb_root)` — total paper count, for subset-mode
  statistics. Read-only.

Each caller catches ImportError if kb_mcp isn't installed
(citations can't be written without the projection DB, but the
caller can still surface that cleanly rather than crashing).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


log = logging.getLogger(__name__)


# ORIGIN enum mirror. Kept here (not imported from kb_citations) so
# kb_mcp remains free of upstream-package imports. The schema
# constraint `origin IN ('frontmatter', 'wikilink', 'mdlink', 'cite',
# 'citation')` is the real source of truth; this constant just names
# the slice owned by citations.
ORIGIN_CITATION = "citation"


def _utc_now_iso() -> str:
    """Timestamp in the same format used by kb_mcp indexer (Z-suffixed
    UTC, second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------
# Citation edges: links table
# ---------------------------------------------------------------------

def apply_citation_edges(
    kb_root: Path,
    edges: Iterable[dict],
) -> None:
    """Atomic full-replace of origin='citation' edges.

    Protocol: this is a *replace* not a merge — every citation edge
    the DB currently holds is deleted, then every edge in `edges` is
    inserted. Empty `edges` means "KB currently has zero citation
    edges" and the DELETE still runs, correctly clearing stale
    prior-run data.

    Each edge dict MUST have keys: src_type, src_key, dst_type,
    dst_key. The `origin` field is forced to 'citation' here — this
    function only writes that slice. Any other keys in the dict
    (e.g. the `meta` sidecar used by kb_citations for JSONL
    fallback) are ignored by design: the `links` table schema is
    intentionally narrow (5 columns) so graph queries stay cheap.

    Raises:
      - ImportError is NOT caught here — caller is expected to
        handle "kb_mcp not installed" themselves. Inside kb_mcp
        we're already imported, so the ImportError path is
        specifically for kb_citations callers whose wrapper catches
        this and falls back to JSONL dump.
    """
    from .store import get_connection

    edges = list(edges)  # allow iterable; we need len() for logging

    conn = get_connection(kb_root)
    try:
        with conn:
            conn.execute(
                "DELETE FROM links WHERE origin = ?",
                (ORIGIN_CITATION,),
            )
            if edges:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO links
                      (src_type, src_key, dst_type, dst_key, origin)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            e["src_type"], e["src_key"],
                            e["dst_type"], e["dst_key"],
                            ORIGIN_CITATION,
                        )
                        for e in edges
                    ],
                )
        log.info(
            "citation edges: wrote %d (cleared prior rows first)",
            len(edges),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Citation counts: papers.citation_count et al.
# ---------------------------------------------------------------------

def update_citation_count(
    kb_root: Path,
    zotero_key: str,
    *,
    count: int | None,
    source: str,
) -> None:
    """Update citation_count columns for one paper.

    Writes three columns in one transaction:
      - papers.citation_count          = count
      - papers.citation_count_source   = source (provider name)
      - papers.citation_count_updated_at = now()

    `count=None` is a valid value (means "provider looked, didn't
    find"); the row still gets the source+timestamp update so we
    don't keep re-querying.
    """
    from .store import get_connection

    conn = get_connection(kb_root)
    try:
        with conn:
            # v26: citation_count is per Zotero item, not per md.
            # For a multi-md work (book + chapter siblings), we
            # write only the whole-work row — identified by
            # paper_key = zotero_key. Chapter rows leave their
            # citation_count columns NULL. This matches the
            # semantics users expect: "this paper has N citations"
            # applies to the work as a whole, not individual chapters.
            conn.execute(
                "UPDATE papers SET "
                "  citation_count = ?, "
                "  citation_count_source = ?, "
                "  citation_count_updated_at = ? "
                "WHERE zotero_key = ? AND paper_key = zotero_key",
                (count, source, _utc_now_iso(), zotero_key),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_papers_with_doi(kb_root: Path) -> list[dict]:
    """Read-only: return [{'key': zotero_key, 'doi': doi}] for every
    Zotero item that has a non-empty DOI. Used by the bulk
    refresh-counts loop in kb_citations.

    v26: filters to whole-work rows only (paper_key = zotero_key),
    so a book split across chapter mds contributes ONE entry (the
    whole-book), not N. Without this filter we'd hit the provider
    once per chapter for a single DOI, wasting API quota.
    """
    from .store import get_connection

    conn = get_connection(kb_root)
    try:
        rows = conn.execute(
            "SELECT zotero_key, doi FROM papers "
            "WHERE doi IS NOT NULL AND doi <> '' "
            "AND paper_key = zotero_key"
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [{"key": r["zotero_key"], "doi": r["doi"]} for r in rows]


def count_papers(kb_root: Path) -> int:
    """Read-only: total paper row count. Used by subset-mode stats
    bookkeeping."""
    from .store import get_connection

    conn = get_connection(kb_root)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM papers"
        ).fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return int(row["n"]) if row else 0
