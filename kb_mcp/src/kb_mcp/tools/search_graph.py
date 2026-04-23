"""Graph-augmented retrieval: hybrid search + citation-neighbor expansion.

Per real-world measurement on a 1154-paper EE/power-electronics KB:
  - baseline search_papers_hybrid recall@10: 45%
  - graph-expanded (seeds → 1-hop citation neighbors): 67%
  - one hard query (PLL weak-grid): 67% → 100%

The mechanism: hybrid seeds often miss foundational or bridge papers
whose titles/abstracts don't contain the query terms but which are
clearly connected via the citation graph. Expanding via one
citation-edge hop (inbound + outbound) captures these.

Cheap: one additional SQL per seed. No extra embedding, no extra LLM.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..store import Store
from .search_hybrid import search_papers_hybrid_impl


log = logging.getLogger(__name__)


def search_papers_graph_impl(
    store: Store,
    kb_root: Path,
    embedder,
    query: str,
    *,
    seed_k: int = 10,
    neighbor_k: int = 20,
    final_k: int = 15,
    min_year: int | None = None,
    max_year: int | None = None,
    require_summary: bool = False,
    query_vector: list[float] | None = None,
) -> str:
    """Two-stage retrieval:

    1. Run hybrid search for `query` → top-`seed_k` papers (seeds).
    2. For each seed, pull citation-edge neighbors (both directions)
       from the `links` table. Merge all neighbors into one candidate
       pool capped at `neighbor_k`.
    3. Return seeds + neighbors, seeds ranked first (they matched
       the query directly), neighbors after (strong topological
       signal but no direct query match).

    Args:
        store, kb_root, embedder: usual kb-mcp plumbing. `embedder`
            is used only as a fallback when `query_vector` is None —
            the server wrapper should pre-embed via its LRU cache
            and pass the vector in directly for best performance.
        query: free-form user query string.
        seed_k: hybrid-search top-K to use as seeds (default 10).
        neighbor_k: max total neighbors to expand to (default 20,
            after dedupe).
        final_k: max rows in final output (default 15).
        min_year/max_year/require_summary: same filters as hybrid
            search, applied to the seed stage only.
        query_vector: precomputed embedding. Highly recommended:
            passing None re-embeds the query inside the hybrid seed
            stage, bypassing the server's cache.

    Output is plain text: ranked list with a tag per row saying
    whether the paper was a seed or a graph-expanded neighbor, plus
    a breadcrumb showing which seed it came from.
    """
    # ---- Stage 1: seeds via hybrid ----
    # If caller didn't provide a query_vector, fall back to embedding
    # here (uncached — prefer the wrapper path).
    if query_vector is None and embedder is not None and store.vec_available:
        try:
            r = embedder.embed([query])
            if r.vectors:
                query_vector = r.vectors[0]
        except Exception as e:
            log.warning("graph seed embed failed (%s); FTS-only seeds", e)

    seed_out = search_papers_hybrid_impl(
        store, query, query_vector,
        limit=seed_k,
        min_year=min_year, max_year=max_year,
        require_summary=require_summary,
    )
    seed_keys = _extract_keys_from_hybrid_output(seed_out)
    if not seed_keys:
        return "(no seeds found by hybrid search)"

    # ---- Stage 2: neighbor expansion via links WHERE origin='citation' ----
    # Both directions: things seed cites, things that cite seed.
    seen = set(seed_keys)
    neighbors: list[tuple[str, str]] = []  # (key, via_seed)
    for sk in seed_keys:
        # Outbound: sk cites X.
        for r in store.execute(
            "SELECT dst_key FROM links "
            "WHERE src_type='paper' AND src_key=? "
            "  AND dst_type='paper' AND origin='citation'",
            (sk,),
        ).fetchall():
            k = r["dst_key"]
            if k in seen:
                continue
            seen.add(k)
            neighbors.append((k, sk))
            if len(neighbors) >= neighbor_k:
                break
        if len(neighbors) >= neighbor_k:
            break
        # Inbound: X cites sk.
        for r in store.execute(
            "SELECT src_key FROM links "
            "WHERE dst_type='paper' AND dst_key=? "
            "  AND src_type='paper' AND origin='citation'",
            (sk,),
        ).fetchall():
            k = r["src_key"]
            if k in seen:
                continue
            seen.add(k)
            neighbors.append((k, sk))
            if len(neighbors) >= neighbor_k:
                break
        if len(neighbors) >= neighbor_k:
            break

    # ---- Stage 3: format ----
    lines = [
        f"graph-augmented search: {len(seed_keys)} seed(s) + "
        f"{len(neighbors)} graph neighbor(s)",
        "",
    ]

    # Pull display metadata for everything we'll show.
    all_keys = list(seed_keys) + [k for k, _ in neighbors]
    all_keys = all_keys[:final_k]
    if not all_keys:
        return "(no results)"

    placeholders = ",".join(["?"] * len(all_keys))
    rows = store.execute(
        f"SELECT paper_key, zotero_key, title, year, citation_count "
        f"FROM papers WHERE paper_key IN ({placeholders})",
        all_keys,
    ).fetchall()
    meta = {r["paper_key"]: dict(r) for r in rows}

    seed_set = set(seed_keys)
    via_map = {k: v for k, v in neighbors}

    for i, k in enumerate(all_keys, 1):
        m = meta.get(k, {})
        title = (m.get("title") or "(no title)")[:65]
        year = m.get("year") or "????"
        cc = m.get("citation_count")
        cc_str = f"c={cc}" if cc is not None else "c=—"
        if k in seed_set:
            tag = "[seed]    "
        else:
            via = via_map.get(k, "?")
            tag = f"[via {via}]"
        lines.append(f"{i:>2}. {tag}  {k}  ({year}, {cc_str})  {title}")

    return "\n".join(lines)


def _extract_keys_from_hybrid_output(out: str) -> list[str]:
    """Parse the formatted output of search_papers_hybrid_impl to
    recover the ranked paper keys.

    Actual hybrid output format (observed):
        "1 result(s) for 'quantum' [FTS-only]:"
        ""
        "  AAAAAAAA  2023     Quantum Journal"
        "      score=0.0167 [FTS]"
        "  BBBBBBBB  2021     Other Paper"
        "      score=0.0156 [hybrid]"

    Keys are 8-10 uppercase alphanumeric tokens that sit at the
    start of an indented row (2 leading spaces, not 6 — the 6-space
    rows are the score detail lines). We use a regex + a check on
    leading whitespace to avoid picking up incidental uppercase
    strings.

    If the format ever changes, re-point this parser. A cleaner
    solution would be to refactor search_papers_hybrid_impl to
    return (formatted_text, structured_rows) — saved for a later
    cleanup round.
    """
    import re
    keys: list[str] = []
    seen: set[str] = set()
    key_re = re.compile(r"^\s{1,4}([A-Z0-9]{8,10})\b")
    for line in out.splitlines():
        m = key_re.match(line)
        if m:
            k = m.group(1)
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys
