"""related_papers: find papers similar to a given paper via vector
similarity.

Algorithm: take the "header" chunk of the anchor paper (title +
abstract as the concise semantic summary), use its embedding as the
query, kNN against all other chunks, dedupe to paper-level, return
top K.

If the anchor has no embedded chunks (not yet embedded, or
embedding unavailable), returns a helpful error.
"""
from __future__ import annotations

import json

from ..store import Store


def related_papers_impl(
    store: Store,
    paper_key: str,
    limit: int = 5,
) -> str:
    """Find papers semantically similar to `paper_key`.

    Uses the anchor paper's header chunk (title + abstract) as the
    query vector. Excludes the anchor from results.
    """
    limit = max(1, min(limit, 50))

    if not store.vec_available:
        return (
            "[error] Vector search unavailable (sqlite-vec not loaded). "
            "Use search_papers_fts for keyword-based related lookup."
        )

    # v26: paper_key is the md stem — for a book split into chapters,
    # you'd pass the specific chapter's paper_key (e.g. "BOOKKEY-ch03")
    # to get neighbours of THAT chapter, or the whole-work key
    # ("BOOKKEY") to get neighbours of the book overall.
    anchor = store.execute(
        "SELECT title, embedded FROM papers WHERE paper_key = ?",
        (paper_key,)
    ).fetchone()
    if anchor is None:
        return f"[not found] No paper with paper_key={paper_key!r}."
    if not anchor["embedded"]:
        return (
            f"[not embedded] Paper {paper_key!r} has no embeddings yet. "
            f"Run `kb-mcp index` to generate them "
            f"(requires OPENAI_API_KEY)."
        )

    # Get the anchor's header chunk embedding. If the header chunk
    # doesn't exist for some reason (all-fulltext paper), fall back
    # to the first available chunk.
    anchor_row = store.execute("""
        SELECT pcm.chunk_id
        FROM paper_chunk_meta pcm
        WHERE pcm.paper_key = ?
        ORDER BY CASE WHEN pcm.kind = 'header' THEN 0 ELSE 1 END,
                 pcm.chunk_id
        LIMIT 1
    """, (paper_key,)).fetchone()
    if anchor_row is None:
        return (
            f"[no chunks] Paper {paper_key!r} is marked embedded but has "
            f"no chunk_meta rows. DB may be inconsistent; try "
            f"deleting .kb-mcp/index.sqlite and re-indexing."
        )
    anchor_chunk_id = anchor_row["chunk_id"]

    # Fetch the anchor vector via vec0's internal access pattern.
    # Use "SELECT embedding FROM paper_chunks_vec WHERE chunk_id = ?".
    vrow = store.execute(
        "SELECT embedding FROM paper_chunks_vec WHERE chunk_id = ?",
        (anchor_chunk_id,)
    ).fetchone()
    if vrow is None:
        return f"[no vector] chunk {anchor_chunk_id} has no vec row."
    anchor_blob = vrow["embedding"]

    # kNN. We pull 10x limit because one anchor paper contributes
    # several chunks (all close to itself, inflating results).
    raw_pool = limit * 10
    # anchor_blob is already a bytes blob — pass through directly.
    rows = store.execute("""
        SELECT pcm.paper_key AS pk, v.distance AS dist
        FROM paper_chunks_vec v
        JOIN paper_chunk_meta pcm ON pcm.chunk_id = v.chunk_id
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        LIMIT ?
    """, (anchor_blob, raw_pool, raw_pool)).fetchall()

    # Dedupe by paper_key, excluding anchor.
    best: dict[str, float] = {}
    for r in rows:
        pk = r["pk"]
        if pk == paper_key:
            continue
        d = r["dist"]
        if pk not in best or d < best[pk]:
            best[pk] = d

    ranked = sorted(best.keys(), key=lambda pk: best[pk])[:limit]
    if not ranked:
        return f"No related papers found for {paper_key!r}."

    placeholders = ",".join("?" * len(ranked))
    meta_rows = {
        r["paper_key"]: r for r in store.execute(
            f"SELECT paper_key, zotero_key, title, year, authors, "
            f"       fulltext_processed "
            f"FROM papers WHERE paper_key IN ({placeholders})",
            tuple(ranked),
        ).fetchall()
    }

    lines = [
        f"Papers related to {paper_key!r} ({anchor['title'] or '(no title)'}):",
        "",
    ]
    for pk in ranked:
        r = meta_rows.get(pk)
        if r is None:
            continue
        year = r["year"] or "?"
        badge = "📝" if r["fulltext_processed"] else "  "
        authors = _short_authors(r["authors"])
        # Lower distance = closer; convert to "similarity" for display.
        # sqlite-vec's default is cosine distance in [0, 2]; similarity
        # = 1 - dist/2 gives roughly [0, 1].
        sim = max(0.0, min(1.0, 1.0 - best[pk] / 2.0))
        lines.append(f"  {pk}  {year}  {badge} {r['title'] or '(no title)'}")
        if authors:
            lines.append(f"      authors: {authors}")
        lines.append(f"      similarity={sim:.3f}")
        lines.append("")
    return "\n".join(lines).rstrip()


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
