"""Extract a model-agnostic similarity prior from the current vector index.

Embeddings from different providers live in incompatible vector spaces
— you can't map an OpenAI vector to a Gemini vector with any fidelity.
What DOES transfer between providers, approximately, is the *relation*
"paper A is close to paper B" — if two papers are genuinely about the
same thing, every decent embedding model will agree they're close.

This module extracts those relations from the current `paper_chunks_vec`
table and writes them to a portable JSON file:

    ee-kb/.kb-mcp/similarity-prior.json

Structure:

    {
      "schema": 1,
      "extracted_at": "2026-04-22T00:00:00Z",
      "extracted_from": {
        "model": "text-embedding-3-small",
        "dim": 1536,
        "paper_count": 1200
      },
      "top_k": 20,
      "neighbors": {
        "ABCD1234": [
          {"key": "EFGH5678", "rank": 1, "distance": 0.18},
          {"key": "IJKL9012", "rank": 2, "distance": 0.22},
          ...
        ],
        ...
      }
    }

Uses:

1. Sanity check after switching providers: extract a new prior from
   the new index, compare via Jaccard. Low overlap = something's off.
2. Warm-start during a reindex: embed high-centrality papers first
   (their neighbors will benefit from faster cache hits on repeated
   queries during warmup).
3. Documentation of what the old model thought was near what —
   useful when debugging "why is this paper suddenly not showing up
   in related_papers".

Design note: we store paper-to-paper distances (aggregated across
chunks via min-distance), not chunk-to-chunk. Chunk IDs don't
survive a reindex; paper keys (zotero_key) do.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..store import Store


log = logging.getLogger(__name__)


PRIOR_SCHEMA_VERSION = 1
PRIOR_FILENAME = "similarity-prior.json"


def extract_similarity_prior(
    store: Store,
    kb_root: Path,
    *,
    top_k: int = 20,
    max_papers: int | None = None,
) -> dict:
    """Scan paper_chunks_vec; for each paper, compute its top-K
    nearest-neighbor papers by minimum chunk distance.

    Returns the full prior dict (caller writes it).

    Args:
        top_k: how many neighbors to record per paper (default 20).
        max_papers: optional cap for testing / huge libraries.
    """
    if not store.vec_available:
        raise RuntimeError(
            "sqlite-vec not loaded; cannot extract similarity prior. "
            "Install sqlite-vec: pip install sqlite-vec"
        )

    # Discover all papers that have vectors.
    papers = store.execute(
        "SELECT DISTINCT pcm.paper_key "
        "FROM paper_chunk_meta pcm "
        "JOIN paper_chunks_vec v ON v.chunk_id = pcm.chunk_id "
        "ORDER BY pcm.paper_key"
    ).fetchall()
    paper_keys = [r["paper_key"] for r in papers]
    if max_papers is not None:
        paper_keys = paper_keys[:max_papers]

    # Detect model + dim of the current index.
    meta_row = store.execute(
        "SELECT embedding_model FROM papers "
        "WHERE embedding_model IS NOT NULL "
        "LIMIT 1"
    ).fetchone()
    model = meta_row["embedding_model"] if meta_row else "unknown"

    # Dim: infer from first vector. sqlite-vec stores dim in the
    # virtual table's metadata; simpler to pick it up from a row.
    dim_row = store.execute(
        "SELECT length(embedding) AS b FROM paper_chunks_vec LIMIT 1"
    ).fetchone()
    # embedding is BLOB of float32s: dim = bytes / 4.
    dim = (dim_row["b"] // 4) if dim_row else None

    neighbors: dict[str, list[dict]] = {}

    # For each paper: pick ONE representative chunk (its first — order
    # of insertion correlates with order of paragraphs; the header
    # chunk is typically chunk 0). Query its top-K neighbors across
    # all other papers. Aggregate to paper level by min-distance.
    #
    # Alternative design: ALL chunks × KNN, aggregate — more accurate
    # but ~10× cost. For a prior (approximate by design) this is plenty.
    for pk in paper_keys:
        # Get first chunk id for this paper.
        chunk_row = store.execute(
            "SELECT chunk_id FROM paper_chunk_meta "
            "WHERE paper_key = ? "
            "ORDER BY chunk_id ASC LIMIT 1",
            (pk,),
        ).fetchone()
        if chunk_row is None:
            continue
        # Fetch its embedding.
        emb_row = store.execute(
            "SELECT embedding FROM paper_chunks_vec WHERE chunk_id = ?",
            (chunk_row["chunk_id"],),
        ).fetchone()
        if emb_row is None:
            continue
        blob = emb_row["embedding"]

        # KNN over every chunk. Pool larger than top_k so we can
        # dedupe to paper-level. Each paper averages ~3 chunks →
        # 3 * top_k covers plenty.
        pool = min(top_k * 4, 200)
        hits = store.execute(
            "SELECT pcm.paper_key AS pk, v.distance AS dist "
            "FROM paper_chunks_vec v "
            "JOIN paper_chunk_meta pcm ON pcm.chunk_id = v.chunk_id "
            "WHERE v.embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (blob, pool),
        ).fetchall()

        # Aggregate to paper level by min-distance; drop self.
        best: dict[str, float] = {}
        for h in hits:
            other = h["pk"]
            if other == pk:
                continue
            d = h["dist"]
            if other not in best or d < best[other]:
                best[other] = d
        # Sort, truncate to top_k.
        top = sorted(best.items(), key=lambda x: x[1])[:top_k]
        neighbors[pk] = [
            {"key": k, "rank": i + 1, "distance": round(float(d), 4)}
            for i, (k, d) in enumerate(top)
        ]

    return {
        "schema": PRIOR_SCHEMA_VERSION,
        "extracted_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "extracted_from": {
            "model": model,
            "dim": dim,
            "paper_count": len(neighbors),
        },
        "top_k": top_k,
        "neighbors": neighbors,
    }


def write_prior(kb_root: Path, prior: dict) -> Path:
    """Persist prior as JSON under .kb-mcp/. Atomic via tmp+rename."""
    out_dir = kb_root / ".kb-mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / PRIOR_FILENAME
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(prior, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(out_path)
    return out_path


def read_prior(kb_root: Path) -> dict | None:
    """Read the prior if it exists; return None otherwise."""
    p = kb_root / ".kb-mcp" / PRIOR_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("prior read failed: %s", e)
        return None


def compare_priors(old: dict, new: dict, *, at_k: int = 10) -> dict:
    """Compare two priors (same KB, different embedding models).

    For each paper present in both, compute Jaccard similarity of
    the top-`at_k` neighbor sets. Reports:

    - `paper_count`: papers present in both priors
    - `mean_jaccard`: average Jaccard across papers
    - `low_overlap`: papers with Jaccard < 0.3 (candidates for
      manual review — the new embedding disagrees strongly with
      the old one about what's similar to these papers)

    Low mean = either the new embedding is significantly worse for
    your corpus (unusual) or it legitimately sees different
    relations (e.g. older model trained mostly on English, new one
    handles your Chinese abstracts better).
    """
    old_n = old.get("neighbors", {})
    new_n = new.get("neighbors", {})
    common_keys = set(old_n) & set(new_n)

    jaccards: list[float] = []
    low_overlap: list[tuple[str, float]] = []

    for pk in sorted(common_keys):
        old_set = {e["key"] for e in old_n[pk][:at_k]}
        new_set = {e["key"] for e in new_n[pk][:at_k]}
        if not old_set and not new_set:
            continue
        union = old_set | new_set
        inter = old_set & new_set
        j = len(inter) / len(union) if union else 0.0
        jaccards.append(j)
        if j < 0.3:
            low_overlap.append((pk, j))

    mean_j = sum(jaccards) / len(jaccards) if jaccards else 0.0
    return {
        "paper_count": len(common_keys),
        "at_k": at_k,
        "mean_jaccard": round(mean_j, 4),
        "old_model": old.get("extracted_from", {}).get("model"),
        "new_model": new.get("extracted_from", {}).get("model"),
        "low_overlap_papers": [
            {"key": k, "jaccard": round(j, 4)}
            for k, j in sorted(low_overlap, key=lambda x: x[1])[:20]
        ],
    }


def high_centrality_keys(prior: dict, *, limit: int = 50) -> list[str]:
    """Return paper keys that appear most often as someone else's
    nearest neighbor.

    These are the "hub" papers — if you're about to do a cold reindex,
    embedding these first means the vec table is "warm" for the
    majority of subsequent neighbor queries early. The indexer can
    consume this list to order its work.
    """
    # Count inbound references.
    in_count: dict[str, int] = {}
    for src, nbrs in prior.get("neighbors", {}).items():
        for n in nbrs:
            k = n["key"]
            in_count[k] = in_count.get(k, 0) + 1

    ranked = sorted(in_count.items(), key=lambda x: -x[1])
    return [k for k, _ in ranked[:limit]]
