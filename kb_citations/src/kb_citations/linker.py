"""Convert cached citation data into edges in kb-mcp's `links` table.

The fetcher produces raw Reference lists in cache. The linker:

1. Walks every cached paper
2. For each reference, tries to resolve it to a local paper_key
3. If resolved AND target is in KB → emit an edge

Edges are written with origin="citation" (a new origin type added
to kb-mcp's link graph semantics), so they're distinguishable from
wikilinks / kb_refs / mdlinks. The existing backlinks /
trace_links tools automatically pick them up.

We update the DB in two phases:
  a) DELETE existing origin="citation" edges (atomic full-replace
     of the citation layer — simpler than incremental diff, and the
     data size is modest).
  b) INSERT all resolved edges.

This runs in a single transaction so a crash mid-way leaves the
previous state intact.

If kb_mcp isn't installed, the linker still runs — it just writes
a diagnostic file (`citations-edges.jsonl`) into the cache dir
instead of the DB, so users without kb-mcp can still inspect the
graph.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .cache import CitationCache
from .provider import Reference
from .resolver import LocalResolver


log = logging.getLogger(__name__)


# Sentinel for origin value. Should match whatever kb-mcp's
# link_extractor emits for citation edges (we add support in kb-mcp
# for origin="citation" so trace_links etc. know about them).
ORIGIN_CITATION = "citation"


@dataclass
class LinkReport:
    cached_papers_scanned: int = 0
    edges_emitted: int = 0
    edges_to_dangling: int = 0      # reference not in local KB
    db_updated: bool = False
    db_error: str | None = None
    fallback_file: Path | None = None   # jsonl path if DB unavailable
    unresolved_samples: list[dict] = field(default_factory=list)


def build_edges(
    kb_root: Path,
    resolver: LocalResolver | None = None,
) -> tuple[list[dict], LinkReport]:
    """Scan cache, emit list of edge dicts.

    Each edge:
        {
          "src_type": "paper",
          "src_key":  "ABCD1234",
          "dst_type": "paper",
          "dst_key":  "EFGH5678",
          "origin":   "citation",
          "meta": {"provider": "semantic_scholar",
                   "ref_doi": "...", "ref_title": "..."}
        }

    v0.27.10: `report.edges_emitted` now counts UNIQUE edges.
    Pre-0.27.10 this was a per-append counter, but the provider's
    "references" and "citations" lists often produce the same
    (src, dst, origin) tuple via different paths (A->X listed in
    A's references AND in X's citations) — the downstream
    `INSERT OR IGNORE` silently collapses those. The old counter
    was larger than the number of rows that actually landed in
    the `links` table, making "wrote N edges" reports misleading.
    """
    cache = CitationCache(kb_root)
    resolver = resolver or LocalResolver.from_kb(kb_root)
    report = LinkReport()
    # Keyed by (src_type, src_key, dst_type, dst_key, origin) so
    # the dedupe matches the downstream UNIQUE constraint on
    # `links`. First-seen wins on meta (preserves the "via"
    # provenance of whichever path discovered the edge first).
    _seen: dict[tuple, dict] = {}

    def _add_edge(edge: dict) -> None:
        key = (edge["src_type"], edge["src_key"],
               edge["dst_type"], edge["dst_key"], edge["origin"])
        if key in _seen:
            return
        _seen[key] = edge

    for src_key in cache.all_keys():
        report.cached_papers_scanned += 1
        data = cache.load(src_key)
        if not data:
            continue
        provider_name = data.get("provider", "")
        # ---- references: A cites X. Direction is A -> X. ----
        for ref in data.get("references") or []:
            dst_key = resolver.resolve(
                doi=ref.get("doi"), title=ref.get("title"),
            )
            if dst_key is None:
                report.edges_to_dangling += 1
                # Keep a few samples for reporting.
                if len(report.unresolved_samples) < 10:
                    report.unresolved_samples.append({
                        "src": src_key,
                        "ref_doi": ref.get("doi"),
                        "ref_title": ref.get("title"),
                    })
                continue
            if dst_key == src_key:
                # Self-citation? Skip — shouldn't happen but defensive.
                continue
            _add_edge({
                "src_type": "paper",
                "src_key":  src_key,
                "dst_type": "paper",
                "dst_key":  dst_key,
                "origin":   ORIGIN_CITATION,
                "meta": {
                    "provider": provider_name,
                    "ref_doi": ref.get("doi"),
                    "ref_title": ref.get("title"),
                    "via": "references",  # A -> X via A's outbound list
                },
            })

        # ---- citations: Y cites A. Direction is Y -> A (reversed). ----
        # Previously these were fetched (at API cost) but never
        # consumed — a real functional gap. If Y is in our local KB,
        # the provider's "papers citing A" list lets us recover the
        # Y -> A inbound edge even when Y's own `references` fetch
        # missed it (or wasn't fetched at all). Duplicate (Y, A)
        # edges across references and citations lists are collapsed
        # by the _seen dict above (so `edges_emitted` matches the
        # unique rows that will land in the DB).
        for cite in data.get("citations") or []:
            src_y = resolver.resolve(
                doi=cite.get("doi"), title=cite.get("title"),
            )
            if src_y is None:
                # Y not in our KB — nothing to point at. Count as
                # dangling but separately so the user can tell
                # "we resolved A but not who cites A".
                report.edges_to_dangling += 1
                if len(report.unresolved_samples) < 10:
                    report.unresolved_samples.append({
                        "src": "(inbound to " + src_key + ")",
                        "ref_doi": cite.get("doi"),
                        "ref_title": cite.get("title"),
                    })
                continue
            if src_y == src_key:
                continue  # self
            _add_edge({
                "src_type": "paper",
                "src_key":  src_y,     # the citer
                "dst_type": "paper",
                "dst_key":  src_key,   # the cited (= the paper we
                                       # fetched this list for)
                "origin":   ORIGIN_CITATION,
                "meta": {
                    "provider": provider_name,
                    "ref_doi": cite.get("doi"),
                    "ref_title": cite.get("title"),
                    "via": "citations",  # Y -> A via A's inbound list
                },
            })

    edges = list(_seen.values())
    report.edges_emitted = len(edges)
    return edges, report


def apply_edges_to_db(kb_root: Path, edges: list[dict]) -> tuple[bool, str | None]:
    """Push edges into kb-mcp's SQLite links table.

    Returns (success, error_msg). On missing kb-mcp, returns
    (False, "kb_mcp not installed").

    v25 change: delegates to `kb_mcp.citation_ops.apply_citation_edges`
    rather than hand-writing SQL. Previously this function knew the
    `links` table schema (column names, origin enum values, INSERT
    OR IGNORE behaviour) directly — meaning any schema change in
    kb_mcp would silently break kb_citations at runtime.  Now the
    only cross-package knowledge is the edge dict shape
    (src_type/src_key/dst_type/dst_key), which is documented by
    `citation_ops.apply_citation_edges`.

    Note on `edge["meta"]` (provider, ref_doi, ref_title, via):
    intentionally NOT persisted to DB. The `links` table schema is
    narrow on purpose so graph queries stay cheap. `meta` exists
    for the JSONL-fallback path (where DB writes failed and we
    dump full edge dicts for humans to diff). If we ever need
    provider attribution inside MCP tools, a side table
    `citation_edge_meta(src_type, src_key, dst_type, dst_key,
    origin, provider, ref_doi, ref_title, via)` is the additive
    path — no migration of existing rows needed. For now YAGNI.
    """
    try:
        from kb_mcp.citation_ops import apply_citation_edges
    except ImportError as e:
        return False, f"kb_mcp not installed: {e}"

    try:
        apply_citation_edges(kb_root, edges)
    except Exception as e:
        return False, f"DB error: {e}"
    return True, None


def link(
    kb_root: Path,
    *,
    fallback_jsonl: bool = True,
) -> LinkReport:
    """Orchestrate: build edges, try DB write, fall back to JSONL.

    Returns a LinkReport summarizing what happened.
    """
    edges, report = build_edges(kb_root)

    # Always go through the DB write path, even when edges is empty.
    # An empty result set still has meaning: "according to the current
    # cache/KB state, there are zero citation edges" — and this needs
    # to be reflected by wiping stale rows from last run. Previously
    # we returned early here, which left the DB holding the union of
    # all historical runs' edges and drifted away from current state.
    # apply_edges_to_db handles edges=[] by doing the DELETE and
    # skipping the executemany insert (empty-sequence is a no-op on
    # executemany, but we check explicitly for clarity).
    if not edges:
        log.info(
            "no edges to write (cache empty or nothing resolved). "
            "Running DELETE-only sync so the DB reflects zero-edge "
            "state rather than stale prior-run edges."
        )

    ok, err = apply_edges_to_db(kb_root, edges)
    if ok:
        report.db_updated = True
        if edges:
            log.info("wrote %d citation edges to kb-mcp DB.",
                     report.edges_emitted)
        else:
            log.info("cleared citation edges from kb-mcp DB "
                     "(zero edges emitted this run).")
    else:
        report.db_error = err
        # v0.28.2: only log the "falling back to JSONL dump" line if
        # we're actually going to produce a fallback file. Otherwise
        # we'd claim fallback and then print "✗ link failed" in the
        # CLI because no file got written. Three cases:
        #   1. edges present + fallback path: real fallback.
        #   2. no edges at all: no-op; don't claim fallback.
        #   3. fallback_jsonl=False: caller opted out.
        if fallback_jsonl and edges:
            log.warning(
                "DB write failed (%s); falling back to JSONL dump.",
                err,
            )
            cache = CitationCache(kb_root)
            cache.ensure_dirs()
            out_path = cache.root / "citation-edges.jsonl"
            with open(out_path, "w", encoding="utf-8") as f:
                for e in edges:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            report.fallback_file = out_path
        elif not edges:
            # Nothing to write. DB failure is inconsequential; log at
            # info so we don't scare the user.
            log.info(
                "DB write failed (%s), but no edges to write anyway; "
                "nothing to do.", err,
            )
        else:
            # fallback_jsonl=False: user-requested strict mode.
            log.warning(
                "DB write failed (%s) and fallback_jsonl=False; "
                "edges lost.", err,
            )

    return report
