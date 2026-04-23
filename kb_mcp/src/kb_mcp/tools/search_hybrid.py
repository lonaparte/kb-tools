"""search_papers_hybrid: combine FTS5 keyword ranking with vector
similarity using Reciprocal Rank Fusion.

When vector search is unavailable (no sqlite-vec, no embedder, no
embeddings yet), we gracefully degrade to FTS5-only — returning the
same output format, with a note in the header line.

Why RRF? Because it needs no score normalization and no learned
weight between scoring systems. Each list contributes 1/(k+rank) per
hit; natural consensus rises to the top. k=60 is the oft-cited
default that works well in practice.
"""
from __future__ import annotations

import json
import struct
from typing import Optional

from ..store import Store


# Number of candidates each backend contributes before RRF fusion.
# Must be >= final limit. Bigger = better recall, slight latency hit.
# 50 handles a library of 1000+ papers comfortably.
CANDIDATE_POOL = 50

# RRF parameter. Standard default.
RRF_K = 60


def search_papers_hybrid_impl(
    store: Store,
    query: str,
    query_vector: Optional[list[float]] = None,
    limit: int = 10,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    require_summary: bool = False,
    item_type: Optional[str] = None,
) -> str:
    """Hybrid keyword + semantic search.

    Args:
        store: projection DB.
        query: user's query string (used for FTS5).
        query_vector: precomputed embedding of the query. If None,
            vector pass is skipped (degrades to FTS5-only).
        limit: max final results.
        min_year / max_year / require_summary: year + summary filters,
            applied in SQL before fusion.
        item_type: optional Zotero item_type filter applied to both
            FTS and vector paths (e.g. "journalArticle", "thesis").
    """
    limit = max(1, min(limit, 100))

    fts_hits = _fts_candidates(
        store, query, min_year, max_year, require_summary, item_type
    )
    vec_hits: list[tuple[str, float]] = []
    used_vector = False
    if query_vector is not None and store.vec_available:
        try:
            vec_hits = _vec_candidates(
                store, query_vector, min_year, max_year, require_summary,
                item_type,
            )
            used_vector = True
        except Exception as e:
            # Graceful degrade: FTS-only if vec query errors.
            vec_hits = []
            used_vector = False

    # RRF fusion.
    scores: dict[str, float] = {}
    for rank, (pk, _) in enumerate(fts_hits):
        scores[pk] = scores.get(pk, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (pk, _) in enumerate(vec_hits):
        scores[pk] = scores.get(pk, 0.0) + 1.0 / (RRF_K + rank)

    if not scores:
        mode = "hybrid" if used_vector else "FTS-only"
        return f"No matches for {query!r} ({mode})."

    # Sort by fused score, fetch display metadata in one query.
    # v26: rows keyed by paper_key (md stem).
    ranked_keys = sorted(scores.keys(), key=lambda k: -scores[k])[:limit]
    placeholders = ",".join("?" * len(ranked_keys))
    rows = {
        r["paper_key"]: r
        for r in store.execute(
            f"SELECT paper_key, zotero_key, title, year, authors, "
            f"       fulltext_processed "
            f"FROM papers WHERE paper_key IN ({placeholders})",
            tuple(ranked_keys),
        ).fetchall()
    }

    # Format output.
    mode = "hybrid (FTS + vector)" if used_vector else "FTS-only"
    header = f"{len(ranked_keys)} result(s) for {query!r} [{mode}]:"
    lines = [header, ""]
    for pk in ranked_keys:
        r = rows.get(pk)
        if r is None:
            continue  # race — paper deleted between rank and fetch
        year = r["year"] or "?"
        badge = "📝" if r["fulltext_processed"] else "  "
        authors = _short_authors(r["authors"])
        lines.append(f"  {pk}  {year}  {badge} {r['title'] or '(no title)'}")
        if authors:
            lines.append(f"      authors: {authors}")
        # Indicate which backends hit this result, for debugging.
        in_fts = any(pk == k for k, _ in fts_hits)
        in_vec = any(pk == k for k, _ in vec_hits)
        tags = []
        if in_fts:
            tags.append("FTS")
        if in_vec:
            tags.append("vec")
        lines.append(f"      score={scores[pk]:.4f} [{'+'.join(tags)}]")
        lines.append("")
    return "\n".join(lines).rstrip()


def _fts_candidates(
    store: Store,
    query: str,
    min_year: Optional[int],
    max_year: Optional[int],
    require_summary: bool,
    item_type: Optional[str] = None,
) -> list[tuple[str, float]]:
    """Run FTS5 and return [(paper_key, bm25_score), ...] in rank order.

    Returns [] on malformed FTS query (graceful degrade — vector side
    may still return results).
    """
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

    sql = f"""
        SELECT paper_fts.paper_key AS pk, bm25(paper_fts) AS score
        FROM paper_fts
        JOIN papers p ON p.paper_key = paper_fts.paper_key
        WHERE {where}
        ORDER BY score
        LIMIT ?
    """
    params.append(CANDIDATE_POOL)
    try:
        rows = store.execute(sql, tuple(params)).fetchall()
    except Exception:
        return []
    return [(r["pk"], r["score"]) for r in rows]


def _vec_candidates(
    store: Store,
    query_vector: list[float],
    min_year: Optional[int],
    max_year: Optional[int],
    require_summary: bool,
    item_type: Optional[str] = None,
) -> list[tuple[str, float]]:
    """Nearest-neighbor via sqlite-vec, deduped to paper level.

    Because each paper has multiple chunks, a kNN over chunks can
    return the same paper several times. We dedupe by keeping only
    the best (smallest distance) hit per paper_key, then re-rank
    among those.
    """
    # Pack vector as float32 bytes — sqlite-vec's expected binary format.
    blob = struct.pack(f"{len(query_vector)}f", *query_vector)

    # Pull a larger pool than CANDIDATE_POOL so that after dedup we
    # still have enough papers. 5× is an empirical factor accounting
    # for most papers having ~5 chunks.
    raw_pool = CANDIDATE_POOL * 5

    # sqlite-vec uses the MATCH operator with a KNN query.
    sql = """
        SELECT pcm.paper_key AS pk, v.distance AS dist
        FROM paper_chunks_vec v
        JOIN paper_chunk_meta pcm ON pcm.chunk_id = v.chunk_id
        JOIN papers p ON p.paper_key = pcm.paper_key
        WHERE v.embedding MATCH ? AND k = ?
    """
    params: list = [blob, raw_pool]
    # Filter clauses applied after the KNN (sqlite-vec restriction:
    # MATCH/k in the WHERE, other filters also in WHERE but after).
    if min_year is not None:
        sql += " AND p.year >= ?"
        params.append(min_year)
    if max_year is not None:
        sql += " AND p.year <= ?"
        params.append(max_year)
    if require_summary:
        sql += " AND p.fulltext_processed = 1"
    if item_type is not None:
        sql += " AND p.item_type = ?"
        params.append(item_type)
    sql += " ORDER BY v.distance LIMIT ?"
    params.append(raw_pool)

    rows = store.execute(sql, tuple(params)).fetchall()

    # Dedupe by paper_key, keeping best distance.
    best: dict[str, float] = {}
    order: list[str] = []
    for r in rows:
        pk = r["pk"]
        d = r["dist"]
        if pk not in best:
            best[pk] = d
            order.append(pk)
        elif d < best[pk]:
            best[pk] = d
    # Order by distance (lower = closer).
    ranked = sorted(order, key=lambda pk: best[pk])[:CANDIDATE_POOL]
    return [(pk, best[pk]) for pk in ranked]


def _short_authors(authors_json: str | None, limit: int = 3) -> str:
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
    return ", ".join(str(a) for a in authors[:limit]) + f", +{len(authors) - limit}"
