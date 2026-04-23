"""Citation layer queries for MCP.

Read-only tools that surface Phase 4 citation data to agents:
- per-paper external count + in/out degree
- global top-cited rankings (by external count or in-degree)
- dangling references aggregated as a reading list

All three read from the projection DB that kb-mcp already manages;
they do NOT hit external APIs. For fetching fresh data, see the
separate `fetch_citations` / `refresh_citation_counts` trigger
tools in server.py.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..store import Store


log = logging.getLogger(__name__)


def paper_citation_stats_impl(store: Store, kb_root: Path, key: str) -> str:
    """Return citation-layer stats for a single paper.

    Args:
        store: kb-mcp projection store.
        kb_root: KB root (for reading citation cache).
        key: Zotero key of the paper.

    Returns human-readable summary:
        title, doi, year
        citation_count (external: Semantic Scholar or OpenAlex)
        in_degree (how many local papers cite this one)
        out_degree (how many local papers this one cites)
        dangling_out (DOIs cited by this paper but NOT in local KB —
                      candidates for Zotero import)
    """
    # v26: citation metadata (count/doi) lives on the whole-work row,
    # whose paper_key equals the Zotero key (no `-chNN` suffix). So
    # looking up by paper_key=key returns the right row.
    row = store.execute(
        "SELECT paper_key, zotero_key, title, year, doi, "
        "       citation_count, citation_count_source, "
        "       citation_count_updated_at "
        "FROM papers WHERE paper_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return f"(no paper with key {key})"

    # Local link graph stats.
    in_deg = store.execute(
        "SELECT COUNT(*) AS c FROM links "
        "WHERE dst_type='paper' AND dst_key=? AND origin='citation'",
        (key,),
    ).fetchone()["c"]
    out_deg = store.execute(
        "SELECT COUNT(*) AS c FROM links "
        "WHERE src_type='paper' AND src_key=? AND origin='citation'",
        (key,),
    ).fetchone()["c"]

    # Dangling out-refs count: read cache JSON if present.
    dangling = _count_dangling_for(kb_root, key, store)

    lines = [
        f"paper: {key}",
        f"  title: {row['title'] or '(no title)'}",
        f"  year:  {row['year'] or '?'}",
        f"  doi:   {row['doi'] or '(no DOI — cannot query providers)'}",
        "",
        "external citation count:",
    ]
    if row["citation_count"] is None:
        lines.append(
            "  (not populated — run `kb-citations refresh-counts` or "
            "the refresh_citation_counts MCP tool)"
        )
    else:
        src = row["citation_count_source"] or "?"
        ts = row["citation_count_updated_at"] or "?"
        lines.append(f"  {row['citation_count']} (per {src}, as of {ts})")

    lines += [
        "",
        "local citation graph:",
        f"  in-degree  (cited by local papers):     {in_deg}",
        f"  out-degree (cites local papers):        {out_deg}",
    ]
    if dangling is not None:
        lines.append(
            f"  dangling out-refs (cited, not local):   {dangling}"
        )
    else:
        lines.append(
            "  dangling out-refs: (no citation cache — run `kb-citations "
            "fetch` or fetch_citations MCP tool)"
        )
    return "\n".join(lines)


def _count_dangling_for(kb_root: Path, key: str, store: Store) -> int | None:
    """Count references from the cache for `key` whose DOI isn't in
    the papers table. Returns None if no cache entry.
    """
    cache_path = kb_root / ".kb-mcp" / "citations" / "by-paper" / f"{key}.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    refs = data.get("references") or []
    if not refs:
        return 0
    # DOIs of local papers (for O(1) membership test)
    local_dois = {
        (r["doi"] or "").lower()
        for r in store.execute(
            "SELECT doi FROM papers WHERE doi IS NOT NULL"
        ).fetchall()
    }
    dangling = sum(
        1 for r in refs
        if r.get("doi") and r["doi"].lower() not in local_dois
    )
    return dangling


def top_cited_papers_impl(
    store: Store,
    limit: int = 20,
    sort_by: str = "citation_count",
    min_year: int | None = None,
) -> str:
    """Return papers ranked by citation metrics.

    Args:
        store: projection store.
        limit: max rows (default 20, cap 100).
        sort_by: "citation_count" (external, from Semantic Scholar or
            OpenAlex) or "in_degree" (internal — how many local
            papers cite this one).
        min_year: optional lower bound on publication year.

    Sorted descending. Papers with NULL citation_count are excluded
    when sort_by="citation_count".
    """
    limit = max(1, min(100, limit))
    if sort_by not in ("citation_count", "in_degree"):
        return (
            f"(invalid sort_by {sort_by!r}; expected "
            f"'citation_count' or 'in_degree')"
        )

    year_clause = ""
    year_params: tuple = ()
    if min_year is not None:
        year_clause = " AND p.year >= ?"
        year_params = (min_year,)

    if sort_by == "citation_count":
        sql = (
            "SELECT p.paper_key, p.zotero_key, p.title, p.year, "
            "       p.citation_count, p.citation_count_source, "
            "       COALESCE(("
            "         SELECT COUNT(*) FROM links l "
            "         WHERE l.dst_type='paper' "
            "           AND l.dst_key=p.paper_key "
            "           AND l.origin='citation'"
            "       ), 0) AS in_degree "
            "FROM papers p "
            "WHERE p.citation_count IS NOT NULL"
            + year_clause +
            " ORDER BY p.citation_count DESC "
            "LIMIT ?"
        )
        rows = store.execute(sql, year_params + (limit,)).fetchall()
    else:  # in_degree
        sql = (
            "SELECT p.paper_key, p.zotero_key, p.title, p.year, "
            "       p.citation_count, p.citation_count_source, "
            "       COUNT(l.dst_key) AS in_degree "
            "FROM papers p "
            "LEFT JOIN links l "
            "     ON l.dst_type='paper' AND l.dst_key=p.paper_key "
            "        AND l.origin='citation' "
            "WHERE 1=1" + year_clause + " "
            "GROUP BY p.paper_key "
            "ORDER BY in_degree DESC, p.citation_count DESC "
            "LIMIT ?"
        )
        rows = store.execute(sql, year_params + (limit,)).fetchall()

    if not rows:
        return "(no papers match — run kb-citations refresh-counts first?)"

    lines = [
        f"top {len(rows)} papers by {sort_by}"
        + (f" (year >= {min_year})" if min_year else "")
        + ":",
        "",
    ]
    for i, r in enumerate(rows, 1):
        cc = (
            f"{r['citation_count']:>5d}" if r["citation_count"] is not None
            else "    —"
        )
        title = (r["title"] or "(no title)")[:70]
        year = r["year"] or "????"
        lines.append(
            f"{i:>2}. [in:{r['in_degree']:>3d}  ext:{cc}]  "
            f"{r['zotero_key']}  ({year})  {title}"
        )
    return "\n".join(lines)


def dangling_references_impl(
    store: Store,
    kb_root: Path,
    limit: int = 50,
    min_cited_by: int = 2,
) -> str:
    """Papers-you-cite-but-don't-own, sorted by in-library citation
    frequency. A reading list pulled from the citation cache.

    Scans `<kb_root>/.kb-mcp/citations/by-paper/*.json`. For each
    reference whose DOI isn't in the local `papers` table, aggregates
    across all source papers to find DOIs cited repeatedly by your
    library — strong signals that those are missing foundational work.

    Args:
        store: projection store (to check DOI membership).
        kb_root: KB root (to locate citation cache).
        limit: max DOIs to return (default 50, cap 200).
        min_cited_by: only show DOIs cited by at least N local papers
            (default 2).

    Output: sorted list of "N × DOI — title (year) — first-author"
    rows. Feed these into Zotero to build out the library.
    """
    limit = max(1, min(200, limit))
    min_cited_by = max(1, min_cited_by)

    cache_dir = kb_root / ".kb-mcp" / "citations" / "by-paper"
    if not cache_dir.exists():
        return (
            "(no citation cache at "
            f"{cache_dir.relative_to(kb_root)} — run `kb-citations "
            "fetch` first)"
        )

    # DOIs of local papers (lowercased, stripped)
    local_dois = {
        r["doi"].lower().strip()
        for r in store.execute(
            "SELECT doi FROM papers WHERE doi IS NOT NULL AND doi <> ''"
        ).fetchall()
    }

    # Aggregate: doi -> (count, sample_title, sample_year, sample_author)
    counts: dict[str, int] = {}
    meta: dict[str, dict] = {}
    for jp in cache_dir.glob("*.json"):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for ref in data.get("references") or []:
            doi = (ref.get("doi") or "").lower().strip()
            if not doi or doi in local_dois:
                continue
            counts[doi] = counts.get(doi, 0) + 1
            if doi not in meta:
                authors = ref.get("authors") or []
                meta[doi] = {
                    "title": ref.get("title") or "",
                    "year": ref.get("year"),
                    "author": authors[0] if authors else "",
                }

    if not counts:
        return (
            "(no dangling references found — either cache is empty, or "
            "all cited DOIs are already in your library)"
        )

    filtered = [(doi, n) for doi, n in counts.items() if n >= min_cited_by]
    filtered.sort(key=lambda x: (-x[1], x[0]))
    filtered = filtered[:limit]

    if not filtered:
        return (
            f"(no dangling refs cited by >= {min_cited_by} local papers; "
            f"{len(counts)} distinct dangling DOIs exist at lower "
            "frequency — lower --min-cited-by to see them)"
        )

    lines = [
        f"top {len(filtered)} dangling references "
        f"(cited by >= {min_cited_by} local papers):",
        "",
    ]
    for doi, n in filtered:
        m = meta.get(doi, {})
        title = (m.get("title") or "")[:60]
        year = m.get("year") or "????"
        author = (m.get("author") or "")[:25]
        lines.append(
            f"  {n:>3}×  {doi:<42}  [{year}] {author:<26}  {title}"
        )

    total_dangling = len(counts)
    if total_dangling > len(filtered):
        lines.append("")
        lines.append(
            f"({total_dangling - len(filtered)} more dangling DOIs at "
            f"lower frequency not shown)"
        )
    return "\n".join(lines)
