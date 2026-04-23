"""search_papers_fts: FTS5-backed keyword search over the papers table.

Phase 2a (this module): pure FTS5. Phase 2b adds a sibling search tool
that combines FTS5 with vector similarity.
"""
from __future__ import annotations

import json
from typing import Optional

from ..store import Store


def search_papers_fts_impl(
    store: Store,
    query: str,
    limit: int = 10,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    require_summary: bool = False,
    item_type: Optional[str] = None,
) -> str:
    """Full-text keyword search over title + authors + abstract + fulltext.

    Returns a formatted list of matches, ordered by FTS5 relevance
    (bm25 default). Each line has paper_key, year, title, and a short
    snippet showing match context.

    Args:
        query: FTS5 query. Supports: plain words ("attention mechanism"),
               phrases ('"small-signal stability"'), boolean (word AND word,
               word OR word, word NOT word), prefix (word*).
        limit: Max rows (default 10, hard cap 100).
        min_year / max_year: Optional year filters. Applied AFTER FTS.
        require_summary: If true, only return papers with
                         fulltext_processed = 1.
        item_type: Optional Zotero item_type filter (e.g.
                   "journalArticle", "conferencePaper", "book",
                   "thesis"). See `papers.item_type` column.
    """
    limit = max(1, min(limit, 100))

    clauses = ["paper_fts MATCH ?"]
    params: list = [query]
    if min_year is not None:
        clauses.append("p.year >= ?")
        params.append(min_year)
    if max_year is not None:
        clauses.append("p.year <= ?")
        params.append(max_year)
    if require_summary:
        clauses.append("p.fulltext_processed = 1")
    if item_type is not None:
        clauses.append("p.item_type = ?")
        params.append(item_type)

    where = " AND ".join(clauses)

    # snippet() returns a context excerpt with the matched term;
    # column index 3 = abstract, 4 = fulltext (in the CREATE order).
    # We pick the best hit column; NULL-safe coalesce.
    sql = f"""
        SELECT
            p.paper_key AS paper_key,
            p.title,
            p.year,
            p.authors,
            p.fulltext_processed,
            bm25(paper_fts) AS score,
            COALESCE(
                snippet(paper_fts, 4, '<<', '>>', ' … ', 16),
                snippet(paper_fts, 3, '<<', '>>', ' … ', 16),
                snippet(paper_fts, 1, '<<', '>>', ' … ', 16)
            ) AS snip
        FROM paper_fts
        JOIN papers p ON p.paper_key = paper_fts.paper_key
        WHERE {where}
        ORDER BY score
        LIMIT ?
    """
    params.append(limit)

    try:
        rows = store.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        # FTS5 rejects malformed queries with a cryptic error. Wrap it.
        return (
            f"[error] FTS query failed: {e}. Hint: quote phrases with "
            f'double quotes (\'"port-hamiltonian"\'), and escape hyphens '
            f"inside words (word1 word2 not word1-word2)."
        )

    if not rows:
        return f"No papers match query {query!r}" + _filter_hint(
            min_year, max_year, require_summary
        )

    lines = [f"{len(rows)} result(s) for {query!r}:", ""]
    for r in rows:
        authors_list = _truncate_authors(r["authors"])
        year = r["year"] or "?"
        has_summary = "📝" if r["fulltext_processed"] else ""
        lines.append(f"  {r['paper_key']}  {year}  {has_summary} {r['title']}")
        if authors_list:
            lines.append(f"      authors: {authors_list}")
        if r["snip"]:
            # Flatten whitespace; FTS5 sometimes returns multi-line.
            snip = " ".join(r["snip"].split())
            lines.append(f"      … {snip}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _filter_hint(min_y, max_y, req_sum) -> str:
    parts = []
    if min_y is not None:
        parts.append(f"min_year={min_y}")
    if max_y is not None:
        parts.append(f"max_year={max_y}")
    if req_sum:
        parts.append("require_summary=True")
    if not parts:
        return "."
    return f" (with filters: {', '.join(parts)})."


def _truncate_authors(authors_json: str | None, limit: int = 3) -> str:
    if not authors_json:
        return ""
    try:
        authors = json.loads(authors_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(authors, list):
        return ""
    if len(authors) <= limit:
        return ", ".join(str(a) for a in authors)
    return ", ".join(str(a) for a in authors[:limit]) + f", +{len(authors) - limit} more"
