"""Bulk-refresh `papers.citation_count` via provider.

Why a dedicated module:

- Reading 1200 papers from the KB's projection DB, hitting a
  provider's paper-metadata endpoint for each, and writing back
  just three columns is a distinct task from fetching the full
  reference graph. It reuses the same provider instance, but runs
  N HTTP calls instead of N * avg_refs.

- This lets the `kb-citations fetch` main loop opportunistically
  update counts (zero extra API cost) AND gives us a standalone
  `kb-citations refresh-counts` for periodic refresh without
  re-pulling citation edges.

Data flow:
    papers table  ──get DOI─▶  provider.get_paper_meta(doi)
                                      │
                                      ▼
                         (citation_count, title, year)
                                      │
                                      ▼
       UPDATE papers SET citation_count = ?, ...
       WHERE zotero_key = ? AND paper_key = zotero_key   -- v26: whole-work only

Missing DOIs / provider miss / 404 are counted but not fatal.

v26 note: citation_count is per Zotero ITEM, not per md. For a
multi-md work (book + chapter siblings), the provider returns one
count for the book's DOI; we write it to the whole-work row only
(identified by paper_key = zotero_key). Chapter rows keep NULL.
The `WHERE ... AND paper_key = zotero_key` filter lives in
kb_mcp.citation_ops.update_citation_count, not here — this module
has zero active SQL (v25 invariant).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .config import CitationsContext
from .provider import CitationProvider, normalize_doi


log = logging.getLogger(__name__)


@dataclass
class RefreshReport:
    total_papers: int = 0
    skipped_no_doi: int = 0
    updated: int = 0
    not_in_provider: int = 0       # DOI returned None from provider
    fetch_errors: int = 0
    db_write_errors: int = 0


# v25: all three helpers below previously hand-wrote SQL against
# kb_mcp's papers table. They now delegate to kb_mcp.citation_ops —
# the narrow boundary module that owns citation-related SQL. This
# removes the "kb_citations knows papers table schema" coupling
# identified in v24's third-party audit. Signatures unchanged so
# the rest of this file doesn't have to move.
# (As part of that migration, the timestamp helper moved to
# kb_mcp.citation_ops too — kb_citations no longer needs its own
# `_iso_utc_now`.)


def _load_papers_with_doi(kb_root: Path) -> list[dict]:
    """Read (zotero_key, doi) for every paper that has a DOI."""
    try:
        from kb_mcp.citation_ops import list_papers_with_doi
    except ImportError as e:
        raise RuntimeError(
            f"kb_mcp not installed; refresh-counts needs it to read "
            f"and write the papers table: {e}"
        )
    return list_papers_with_doi(kb_root)


def _total_paper_count(kb_root: Path) -> int:
    from kb_mcp.citation_ops import count_papers
    return count_papers(kb_root)


def _write_count(
    kb_root: Path,
    zotero_key: str,
    *,
    count: int | None,
    source: str,
) -> None:
    from kb_mcp.citation_ops import update_citation_count
    update_citation_count(
        kb_root, zotero_key, count=count, source=source,
    )


def refresh_counts(
    ctx: CitationsContext,
    provider: CitationProvider,
    *,
    progress: Callable[[str], None] | None = None,
    max_api_calls: int | None = None,
    only_keys: list[str] | None = None,
) -> RefreshReport:
    """Iterate every paper with a DOI; update citation_count from
    provider.

    Args:
        ctx: workspace context (kb_root is used).
        provider: built provider (SemanticScholar / OpenAlex).
        progress: per-paper callback; defaults to stderr.
        max_api_calls: hard cap on provider calls. Defaults to None
            (unlimited, rate-limited only by provider's own throttle).
            Pass a number to bound the run when the provider is
            flaky or you're just sampling.
        only_keys: if given, restrict to just these Zotero keys.
            Used by the MCP `refresh_citation_counts` tool for
            agent-scoped refreshes.

    Returns RefreshReport. Does NOT raise on individual failures —
    those increment `fetch_errors` / `not_in_provider` / `db_write_errors`
    and the run continues.
    """
    if progress is None:
        progress = lambda s: print(s, file=sys.stderr, flush=True)

    report = RefreshReport()
    papers = _load_papers_with_doi(ctx.kb_root)

    # Same subset-accounting fix as fetch_all: when only_keys is set,
    # `total_papers` and `skipped_no_doi` are reported against the
    # REQUESTED subset so they're meaningful. Previously the code
    # computed skipped_no_doi against the whole library then filtered
    # to subset — producing impossible numbers like
    # "total=1154 / skipped_no_doi=1134" for a 20-key request.
    if only_keys is not None:
        wanted = set(only_keys)
        papers = [p for p in papers if p["key"] in wanted]
        report.total_papers = len(wanted)
        report.skipped_no_doi = len(wanted) - len(papers)
        progress(
            f"refresh-counts: subset of {len(wanted)} requested, "
            f"{len(papers)} matched with DOI, provider={provider.name}"
        )
    else:
        report.total_papers = _total_paper_count(ctx.kb_root)
        report.skipped_no_doi = report.total_papers - len(papers)
        progress(
            f"refresh-counts: {report.total_papers} papers total, "
            f"{len(papers)} with DOI, provider={provider.name}"
        )
    if max_api_calls is not None and max_api_calls < len(papers):
        progress(
            f"  (limiting to first {max_api_calls} papers per --max-api-calls)"
        )
        papers = papers[:max_api_calls]

    for i, p in enumerate(papers, start=1):
        prefix = f"[{i}/{len(papers)}] {p['key']}"
        try:
            meta = provider.get_paper_meta(p["doi"])
        except Exception as e:
            report.fetch_errors += 1
            progress(f"{prefix}: fetch error {e!r}")
            continue

        if meta is None:
            report.not_in_provider += 1
            progress(f"{prefix}: not in provider corpus")
            continue

        count = meta.get("citation_count")
        try:
            _write_count(
                ctx.kb_root, p["key"],
                count=count, source=provider.name,
            )
        except Exception as e:
            report.db_write_errors += 1
            progress(f"{prefix}: DB write error: {e}")
            continue

        report.updated += 1
        progress(f"{prefix}: cited_by={count}")

    progress(
        f"done: updated={report.updated} "
        f"not_in_provider={report.not_in_provider} "
        f"fetch_errors={report.fetch_errors} "
        f"db_errors={report.db_write_errors}"
    )
    return report


# Note: an earlier draft of this module exposed an
# `opportunistic_update_from_meta(kb_root, paper_key, ...)` helper
# with the idea that `fetch_all`'s references/citations API calls
# might happen to return citationCount in the same response body,
# letting us update `papers.citation_count` at zero extra cost.
#
# In practice neither OpenAlex's `/works/{id}/references` nor
# Semantic Scholar's `/paper/.../references` include the subject
# paper's citationCount in the response — it's only available from
# `get_citation_meta()` (a separate endpoint hit). So the
# "zero-cost" premise was wrong, and the helper was never hooked
# into the fetch loop. Removed in v24 to prevent confusion; the
# canonical way to update citation_count remains
# `kb-citations refresh-counts`, which explicitly calls
# `get_citation_meta` and pays the extra API call per paper.
