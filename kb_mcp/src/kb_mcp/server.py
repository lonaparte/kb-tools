"""kb-mcp CLI: `serve` (MCP server) and `index` (refresh projection DB).

Phase 2a adds:
- `kb-mcp index` as an explicit subcommand.
- A module-level Store that MCP tool wrappers reach through.
- Three new MCP tools backed by the projection DB:
  `find_paper_by_attachment_key`, `search_papers_fts`, `index_status`.
- Lazy reindex: before every MCP tool call, a quick scan updates stale
  rows. This keeps the DB fresh without a daemon.

Tool descriptions (docstrings) are the most important part of the tool
wrappers — they're the AI's only signal for *when* to use each tool.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Config, ConfigError, load_config
from .embedding import build_from_config, SUPPORTED_PROVIDERS
from .indexer import Indexer
from .store import Store, default_db_path
from .tools.find import find_paper_by_key_impl
from .tools.grep import grep_md_impl
from .tools.index_status import index_status_impl
from .tools.list import list_files_impl
from .tools.read import read_md_impl
from .tools.related import related_papers_impl
from .tools.reverse_lookup import find_paper_by_attachment_key_impl
from .tools.search_fts import search_papers_fts_impl
from .tools.search_hybrid import search_papers_hybrid_impl
from .tools.search_graph import search_papers_graph_impl
from .tools.backlinks import backlinks_impl
from .tools.trace_links import trace_links_impl
from .tools.agent_prefs import get_agent_preferences_impl
from .tools.citation_stats import (
    paper_citation_stats_impl,
    top_cited_papers_impl,
    dangling_references_impl,
)
from .server_cli import (
    _positive_int,
    _setup_logging,
    build_parser,
    _cmd_fetch_citations_impl,
    _cmd_link_citations_impl,
    _cmd_refresh_counts_impl,
)

# Write tools require kb_write. If it isn't installed, we still start
# the server (read-only mode); only the write tools become
# unavailable.
try:
    from .tools import write_ops
    _WRITE_OK = True
except ImportError as _write_import_err:
    # kb_write not installed. Server still starts; write tools
    # become unavailable. Use a logger directly since module-level
    # `log` hasn't been created yet at this point.
    logging.getLogger(__name__).warning(
        "kb_write not installed; write MCP tools unavailable (%s)",
        _write_import_err,
    )
    _WRITE_OK = False
    write_ops = None

log = logging.getLogger(__name__)


# Module-level state set by main() before the server starts.
_cfg: Config | None = None
_store: Store | None = None
# Phase 2b: optional embedding provider (OpenAI or similar). None if
# disabled in config, no API key, or provider unavailable — in which
# case hybrid search degrades to FTS-only.
_embedder = None  # EmbeddingProvider | None


def _kb_root() -> Path:
    if _cfg is None:
        raise RuntimeError("Server not initialized; call main() first.")
    return _cfg.kb_root


def _store_obj() -> Store:
    if _store is None:
        raise RuntimeError("Store not initialized.")
    return _store


def _emit_index_op_event(
    kb_root: Path, *, subcommand: str, rc: int,
    extra: dict | None = None,
) -> None:
    """Record a single INDEX_OP event for a big kb-mcp operation:
    `reindex --force`, `snapshot export`, `snapshot import`.

    Best-effort. Ordinary incremental `kb-mcp index` is NOT logged —
    it runs on every lazy-reindex inside MCP tool calls and would
    drown everything else. Only deliberate operator actions land
    here.
    """
    try:
        from kb_importer.events import (
            record_event, EVENT_INDEX_OP,
            INDEX_OP_OK, INDEX_OP_FAILED,
        )
    except ImportError:
        return
    try:
        category = INDEX_OP_OK if rc == 0 else INDEX_OP_FAILED
        merged_extra = {"subcommand": subcommand}
        if extra:
            merged_extra.update(extra)
        record_event(
            kb_root,
            event_type=EVENT_INDEX_OP,
            category=category,
            detail=f"{subcommand}: rc={rc}",
            pipeline="kb_mcp",
            extra=merged_extra,
        )
    except Exception:
        pass


# v0.27.5: rate-limit lazy_reindex to avoid per-tool-call memory
# churn during agent bursts. On a 1154-paper library each
# lazy_reindex does ~1344 stat() calls + a burst of per-paper SQL
# reads. tracemalloc on a 20-call run shows Python-object growth is
# tiny (<10 KB total); the ~156 KB/call RSS growth measured in the
# field is almost entirely SQLite's C-level page + statement cache
# plus pymalloc arena retention — freed memory the allocator
# doesn't return to the OS. Skipping the reindex when we ran one
# < 1s ago caps the churn without meaningfully affecting
# freshness: back-to-back tool calls in an agent burst don't
# change md state between calls, and kb-write's own
# `trigger_reindex` is an out-of-process subprocess that updates
# the DB independently.
#
# Environment override (testing / very-rapid-edit workflows):
#   KB_MCP_LAZY_REINDEX_COOLDOWN_S=<float>   e.g. 0 = always run
try:
    _LAZY_REINDEX_COOLDOWN_S = float(
        os.environ.get("KB_MCP_LAZY_REINDEX_COOLDOWN_S", "1.0")
    )
except ValueError:
    _LAZY_REINDEX_COOLDOWN_S = 1.0
_last_lazy_reindex_at: float | None = None


# v0.27.5 second lever: periodic glibc malloc_trim to release
# freed-but-retained arenas back to the OS. Field profile showed
# +24 MB RSS / 90 tool calls with the TTL cooldown alone (Python-
# object growth ~10 KB; the rest is sqlite page cache + pymalloc
# arenas). malloc_trim(0) instructs glibc to shrink the heap past
# its high-water mark; a synthetic 80 MB → 13 MB benchmark showed
# ~8 MB of retained memory reclaimed per call.
#
# We can't call it on every tool (~1 ms syscall each, plus the
# page-fault thrash when the arenas rebuild); a burst-aware cadence
# works well — trim every Nth lazy_reindex, covering the common
# "agent fires 10-30 tools then idles" pattern without affecting
# per-call latency. Non-glibc platforms (musl, macOS) lack this
# symbol — we detect once and become a no-op there.
#
# Tunable via KB_MCP_MALLOC_TRIM_EVERY=<int>  (0 = disable, default 16)
try:
    _MALLOC_TRIM_EVERY = int(os.environ.get("KB_MCP_MALLOC_TRIM_EVERY", "16"))
except ValueError:
    _MALLOC_TRIM_EVERY = 16
_tool_call_counter = 0

def _init_malloc_trim():
    """Resolve glibc's malloc_trim once. Returns a callable that
    reclaims arenas, or None when the platform's libc doesn't
    export the symbol."""
    if _MALLOC_TRIM_EVERY <= 0:
        return None
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=False)
        if not hasattr(libc, "malloc_trim"):
            return None
        libc.malloc_trim.argtypes = [ctypes.c_size_t]
        libc.malloc_trim.restype = ctypes.c_int
        return libc.malloc_trim
    except Exception:
        # Non-glibc (musl, macOS) — silently disable.
        return None

_malloc_trim = _init_malloc_trim()


def _maybe_trim_arenas() -> None:
    """Called from _lazy_reindex on every DB-backed tool. Every Nth
    call fires malloc_trim + gc.collect to push freed memory back
    to the OS. Cheap no-op on non-glibc platforms."""
    global _tool_call_counter
    if _malloc_trim is None:
        return
    _tool_call_counter += 1
    if _tool_call_counter >= _MALLOC_TRIM_EVERY:
        _tool_call_counter = 0
        import gc
        gc.collect()
        try:
            _malloc_trim(0)
        except Exception:
            log.debug("malloc_trim raised; ignoring", exc_info=True)


def _lazy_reindex() -> None:
    """Quick scan to ensure DB reflects recent md edits.

    Called at the top of every DB-backed MCP tool. For Phase 2a the
    cost is a full-tree walk (~50ms for 1000 md files); mostly
    unchanged rows skip I/O thanks to mtime comparison.

    Phase 2b: if the provider is available, newly-indexed papers
    also get embedded. A lazy_reindex during a query shouldn't
    block on API calls for huge batches — so in practice this fires
    for small diffs (1-5 papers) between explicit `kb-mcp index` runs.

    We don't call this on the 4 Phase 1 tools (find/list/read/grep)
    because they work directly off the filesystem and don't care
    about the DB.

    v0.27.5: TTL cooldown — if we ran a reindex < COOLDOWN_S ago,
    skip. Big memory-churn reduction without freshness loss in the
    common agent-burst case. Override via
    KB_MCP_LAZY_REINDEX_COOLDOWN_S env var (0 = always run).
    """
    global _last_lazy_reindex_at
    # Periodic arena trim runs regardless of the cooldown skip —
    # it's the only place that sees every DB-backed tool call, so
    # it's the natural hook for "free retained memory every N
    # calls". Fires before the cooldown check so even skipped
    # reindexes count toward the trim cadence.
    _maybe_trim_arenas()
    if _cfg is None or _store is None:
        return
    if _LAZY_REINDEX_COOLDOWN_S > 0 and _last_lazy_reindex_at is not None:
        if time.monotonic() - _last_lazy_reindex_at < _LAZY_REINDEX_COOLDOWN_S:
            return
    try:
        Indexer(
            _cfg.kb_root, _store,
            embedding_provider=_embedder,
            embedding_batch_size=_cfg.embedding_batch_size,
        ).reindex_if_stale()
        _last_lazy_reindex_at = time.monotonic()
    except Exception:
        # Don't let indexing errors block reads — log and proceed.
        # Do NOT stamp _last_lazy_reindex_at on failure — a failed
        # scan should retry on the next call, not honour the
        # cooldown and mask further errors.
        log.exception("lazy reindex failed; serving stale DB")


# ---------------------------------------------------------------------
# Query embedding cache
# ---------------------------------------------------------------------
# In-session duplicate queries ("pll stability", "impedance shaping")
# are common, especially when an agent loops over retrieve-reason-refine.
# Gemini embeds at ~200ms; a 30% hit rate buys back 60ms per query avg.
#
# Cache key is (model_name, query_text). Model name switches (e.g. via
# reindex --force --provider X) invalidate via the key so we never
# return stale vectors from a prior provider.
#
# Simple OrderedDict LRU — bounded 128 entries (~1.5 MB @ 1536 dim
# float + overhead), scoped per-process so the MCP server picks up
# a fresh cache on restart.

from collections import OrderedDict

_QUERY_EMBED_CACHE_MAX = 128
_query_embed_cache: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()


def _embed_query_cached(query: str) -> list[float] | None:
    """Embed `query` with the current provider, using an LRU cache.

    Returns None if embedding is unavailable (no embedder, no
    sqlite-vec, or API failure — caller degrades to FTS-only).
    """
    if _embedder is None or _store is None or not _store.vec_available:
        return None

    # Model name as part of key so provider/model switches don't
    # accidentally serve stale vectors.
    model = getattr(_embedder, "model_name", "unknown")
    key = (model, query)

    hit = _query_embed_cache.get(key)
    if hit is not None:
        _query_embed_cache.move_to_end(key)  # mark recently used
        return hit

    try:
        result = _embedder.embed([query])
    except Exception as e:
        log.warning("Query embedding failed (%s); degrading to FTS only.", e)
        return None
    if not result.vectors:
        return None

    vec = result.vectors[0]
    _query_embed_cache[key] = vec
    if len(_query_embed_cache) > _QUERY_EMBED_CACHE_MAX:
        _query_embed_cache.popitem(last=False)  # evict LRU
    return vec



# ---------------------------------------------------------------------
# FastMCP tool registrations
# ---------------------------------------------------------------------

mcp = FastMCP("ee-kb")


@mcp.tool()
def find_paper_by_key(zotero_key: str) -> str:
    """Fast, deterministic lookup by Zotero key. Try this FIRST when you
    have any concrete reference to a paper — from earlier in the
    conversation, from list_files output, or from an explicit user
    mention like "the ABCD1234 paper".

    Returns the complete markdown (frontmatter + body) including
    metadata, Zotero notes, and the AI notes region. < 50 ms.

    Returns a "[not found]" message if the key doesn't match any paper
    or standalone note md. Does NOT fall back to semantic search — if
    you need that, use search_papers_fts.

    v26: If the key is the parent of a multi-md work (e.g. a book
    split into chapter mds like `BOOKKEY-ch01.md`, `BOOKKEY-ch02.md`),
    this returns the PARENT whole-work md only. Use `list_paper_parts`
    to enumerate the chapter siblings.

    Args:
        zotero_key: An 8-character Zotero item key, uppercase alphanum.
    """
    return find_paper_by_key_impl(_kb_root(), zotero_key)


@mcp.tool()
def list_paper_parts(zotero_key: str) -> str:
    """List all md files under papers/ that share a given Zotero key.

    v26+: a single Zotero item may correspond to multiple mds when
    the work is split across parts — typically a book or long thesis
    with per-chapter mds named `<KEY>-chNN.md` alongside the whole-
    work `<KEY>.md`. This tool returns the complete list so an
    agent can walk through all parts of a work.

    For the common case (single-md paper), returns just one path.

    Returns a multi-line human-readable listing. Use the returned
    paths with `read_md` to get each part's full content.

    Args:
        zotero_key: The parent Zotero key (the part before any `-ch`
                    suffix). E.g. "BOOKKEY", not "BOOKKEY-ch03".
    """
    from .tools.find import list_paper_parts_impl
    return list_paper_parts_impl(_kb_root(), zotero_key)


@mcp.tool()
def list_files(
    subdir: str = "",
    kind_filter: str | None = None,
    limit: int = 100,
) -> str:
    """Fast, deterministic directory listing. Use this to orient
    yourself in the KB ("what's in topics/?", "what papers do we have?")
    or to find recently-added files by browsing.

    Do NOT use this as a search — it returns filenames, not content.
    Use grep_md or search_papers_fts for content-based search.

    Args:
        subdir: Relative to KB root. E.g. "papers", "topics/attention".
                Empty = whole KB.
        kind_filter: Optional. Only return md files whose frontmatter
                     has `kind: <this>`. One of: "paper", "note"
                     (or the legacy "zotero_standalone_note" —
                     accepted for backward compat with pre-v27
                     imports), "topic", "thought".
                     Slower (reads each file's frontmatter).
        limit: Max rows to return (default 100, cap 500).
    """
    return list_files_impl(_kb_root(), subdir, kind_filter, limit)


@mcp.tool()
def read_md(md_path: str) -> str:
    """Fast, deterministic file read. Use this once you know the exact
    md path (e.g. from find_paper_by_key, list_files, or a search hit).

    Returns the full file content, **prefixed with a single HTML
    comment line** of the form `<!-- mtime: FLOAT -->` so that agents
    can pass `expected_mtime` to `update_*` write tools without an
    extra stat call. Strip that prefix line before consuming the
    content.

    < 50 ms for typical files. Refuses files > 2 MB.

    Args:
        md_path: Path relative to the KB root, e.g.
                 "papers/ABCD1234.md" or "topics/attention/overview.md".
                 Absolute paths and paths escaping the KB root are
                 rejected.
    """
    return read_md_impl(_kb_root(), md_path)


@mcp.tool()
def grep_md(
    pattern: str,
    scope: list[str] | None = None,
    limit: int = 20,
) -> str:
    """Case-insensitive literal substring search across md files. Use
    when search_papers_fts doesn't apply — e.g. searching topics/ or
    thoughts/ which aren't in FTS5, or searching for exact literal
    phrases that FTS5 tokenization might miss.

    Multiple space-separated terms are ANDed (all must appear in the
    file). This is NOT regex or semantic search — it's `grep -i -l`
    with excerpts.

    Prefer search_papers_fts for:
      - Paper-only searches.
      - Queries with boolean logic / phrase matching.
      - Year / summary-status filters.

    Args:
        pattern: Space-separated terms. All must appear (case-insensitive)
                 in each matched file.
        scope: Optional list of subdirs to search (e.g.
               ["papers", "topics"]). Empty = whole KB.
        limit: Max files to return (default 20, cap 100).
    """
    return grep_md_impl(_kb_root(), pattern, scope, limit)


@mcp.tool()
def find_paper_by_attachment_key(attachment_key: str) -> str:
    """Look up the parent paper given a Zotero attachment key (e.g. a
    storage/ subdir name). Use when you've encountered an 8-char key
    you're NOT sure is a paper key — could be an attachment.

    O(1) lookup through the projection DB's paper_attachments index.
    Returns paper_key, title, year, authors, and whether this is the
    "main" PDF among the paper's attachments.

    Fast way to disambiguate keys before calling set-summary,
    find_paper_by_key, or read_md.

    Args:
        attachment_key: 8-char uppercase alphanum, e.g. "UUZRAV8C".
    """
    _lazy_reindex()
    return find_paper_by_attachment_key_impl(_store_obj(), attachment_key)


@mcp.tool()
def search_papers_fts(
    query: str,
    limit: int = 10,
    min_year: int | None = None,
    max_year: int | None = None,
    require_summary: bool = False,
    item_type: str | None = None,
) -> str:
    """Full-text keyword search over papers (title + authors + abstract
    + AI summary). Use for content questions like "which papers discuss
    port-Hamiltonian" or "find Smith 2023 on stochastic stability".

    Uses SQLite FTS5 (bm25 ranking). Supports:
    - plain words: `port-hamiltonian stability`
    - phrases: `"small-signal stability"`
    - boolean: `attention AND transformer NOT RNN`
    - prefix: `converge*`

    Returns ranked matches with snippets showing where the terms hit.

    NOT semantic search — if your query is conceptual (e.g. "papers
    like this one" by meaning), you want search_papers_hybrid
    (Phase 2b) or related_papers. Use grep_md for non-paper content.

    Args:
        query: FTS5 query string.
        limit: Max results (default 10, cap 100).
        min_year, max_year: Optional year filters.
        require_summary: If true, only return papers with an AI
                         summary (fulltext_processed=true). Very useful
                         for questions that need detailed content.
        item_type: Optional Zotero item type filter. Common values:
                   "journalArticle", "conferencePaper", "book",
                   "bookSection", "thesis", "report", "preprint".
                   Case-sensitive.
    """
    _lazy_reindex()
    return search_papers_fts_impl(
        _store_obj(), query, limit, min_year, max_year, require_summary,
        item_type=item_type,
    )


@mcp.tool()
def search_papers_hybrid(
    query: str,
    limit: int = 10,
    min_year: int | None = None,
    max_year: int | None = None,
    require_summary: bool = False,
    item_type: str | None = None,
) -> str:
    """Hybrid keyword + semantic search across papers. PREFER this over
    search_papers_fts for conceptual queries like "passivity-based
    control of grid-forming converters" or "small-signal stability
    with large renewable share".

    Fuses FTS5 keyword ranking with embedding-based vector similarity
    via Reciprocal Rank Fusion. Hits high in BOTH backends rise to
    the top, giving keyword precision + semantic recall.

    Falls back to pure FTS automatically if vector search is
    unavailable (no API key, no embeddings yet, etc.). The output
    header line tells you which mode was used.

    Use search_papers_fts instead when:
    - Query is a specific technical term / acronym (e.g. "IEEE 1547").
    - You want boolean logic (AND/OR/NOT, phrase matching).
    - You're minimizing OpenAI API cost (hybrid embeds each query).

    Args:
        query: natural-language query. Sentences work well.
        limit: max results (default 10, cap 100).
        min_year, max_year, require_summary: filters.
        item_type: Optional Zotero item type filter. Common values:
                   "journalArticle", "conferencePaper", "book",
                   "bookSection", "thesis", "report", "preprint".
    """
    _lazy_reindex()
    qvec = _embed_query_cached(query) if _embedder is not None else None
    return search_papers_hybrid_impl(
        _store_obj(), query, qvec, limit, min_year, max_year,
        require_summary, item_type=item_type,
    )


@mcp.tool()
def search_papers_graph(
    query: str,
    seed_k: int = 10,
    neighbor_k: int = 20,
    final_k: int = 15,
    min_year: int | None = None,
    max_year: int | None = None,
    require_summary: bool = False,
) -> str:
    """Graph-augmented retrieval: hybrid search + 1-hop citation
    neighbors.

    Flow: runs `search_papers_hybrid` to get `seed_k` seeds, then
    pulls every paper connected to those seeds by a citation edge
    (both inbound and outbound) from the `links` table, dedupes, and
    returns seeds-first followed by neighbors up to `final_k` total.

    When to prefer this over plain hybrid: concept queries where
    the foundational / bridge paper likely doesn't contain the query
    terms verbatim but is clearly connected via references. Measured
    recall improvement on a 1154-paper EE KB: 45% → 67% @ K=10+20.
    For one query (PLL weak-grid): 67% → 100%.

    Requires citation edges to exist — run `kb-citations fetch` and
    `kb-citations link` first (or the fetch_citations /
    link_citations MCP tools). Without them this degrades to just
    hybrid output + 0 neighbors.

    Args:
        query: free-form text.
        seed_k: how many hybrid-search top-K papers to treat as
            seeds (default 10).
        neighbor_k: max graph-expanded neighbors (default 20, after
            dedup). Neighbors are traversed seed-by-seed, so you
            get balanced coverage across seeds.
        final_k: max rows in the final output (default 15).
        min_year/max_year/require_summary: filters applied during
            the hybrid seed stage only. Neighbors are not re-filtered
            (the intent is to find loosely-connected papers).
    """
    _lazy_reindex()
    if _embedder is None:
        return (
            "error: no embedder configured; graph search needs hybrid "
            "seeds which need embeddings. Fall back to search_papers_fts "
            "+ backlinks for a manual equivalent."
        )
    qvec = _embed_query_cached(query)
    return search_papers_graph_impl(
        _store_obj(), _kb_root(), _embedder, query,
        seed_k=seed_k, neighbor_k=neighbor_k, final_k=final_k,
        min_year=min_year, max_year=max_year,
        require_summary=require_summary,
        query_vector=qvec,
    )


@mcp.tool()
def related_papers(paper_key: str, limit: int = 5) -> str:
    """Find papers semantically similar to a given paper. Uses the
    paper's title + abstract embedding as the query; returns top K
    neighbors by cosine similarity.

    Useful for "what else should I read alongside this one?" — often
    surfaces papers with little keyword overlap but similar conceptual
    framing.

    Anchor paper must have embeddings. If it doesn't, returns an
    error pointing you to `kb-mcp index`.

    Args:
        paper_key: Zotero key of the anchor paper.
        limit: max related papers (default 5, cap 50).
    """
    _lazy_reindex()
    return related_papers_impl(_store_obj(), paper_key, limit)


@mcp.tool()
def backlinks(target: str) -> str:
    """Who references this paper / note / topic / thought?

    Returns all incoming links — other nodes that point here via
    frontmatter kb_refs, [[wikilinks]], [markdown](links.md), or
    @citation_keys (papers only). Use to discover unexpected uses:
    "this paper I'm reading, which topic notes cite it?"; "the
    topic I just wrote, which thoughts built on it?".

    Grouped by source node type for readability. Each line shows
    which origin(s) contributed the edge (a single src→dst can
    appear via multiple origins — e.g. both kb_refs and wikilink
    confirm each other; that gets rendered as [frontmatter+wikilink]).

    Args:
        target: Preferred form: KB-relative path like
            "papers/ABCD1234.md" or "topics/gfm-stability.md".
            Bare key/slug also works (e.g. "ABCD1234") but
            disambiguates less well — if two node types share a
            key it searches all.
    """
    _lazy_reindex()
    return backlinks_impl(_store_obj(), target)


@mcp.tool()
def trace_links(
    start: str,
    depth: int = 2,
    direction: str = "out",
) -> str:
    """BFS the link graph from a starting node, showing reachable
    nodes up to `depth` hops away.

    Use for "show me everything connected to topics/gfm-stability
    within 2 hops" or "what chain of thoughts leads to this paper?".
    The output is a depth-indented tree; cycles are pruned via a
    visited set. Dangling edges (ref pointed at a node that doesn't
    exist) are shown at depth but not expanded further.

    Args:
        start: Node to start from. Accepts path form
            ("papers/ABCD1234.md"), type form ("paper/ABCD1234"),
            or bare key (tries all node types to disambiguate).
        depth: Number of hops (1-4, default 2). Higher = more
            context but longer output.
        direction: "out" (follow references I make), "in"
            (follow references TO me — like backlinks but
            transitive), or "both" (undirected neighborhood).
    """
    _lazy_reindex()
    return trace_links_impl(_store_obj(), start, depth, direction)


@mcp.tool()
def get_agent_preferences(scope: str = "all") -> str:
    """Read the user's persistent preferences and quirks. CALL THIS
    FIRST at the start of any conversation where you will be doing
    substantive work with the user (reading papers, writing notes,
    discussing their research, generating summaries).

    These files encode the user's style rules, research context,
    and task-specific conventions that apply ACROSS conversations.
    Applying them silently saves the user from having to restate
    context every session.

    Returns concatenated content of every `.md` file under
    `<kb_root>/.agent-prefs/`, with per-file frontmatter headers
    showing scope and priority. Apply the narrower-scope prefs over
    global ones; in-conversation user instructions override all prefs.

    Args:
        scope: Optional. If "all" (default), returns all preferences.
            If set to a specific scope tag (e.g. "writing",
            "research", "ai-summary"), only returns files whose
            frontmatter declares that scope.
    """
    return get_agent_preferences_impl(_kb_root(), scope)


# ----------------------------------------------------------------------
# Citation layer — read tools (Phase 4)
# ----------------------------------------------------------------------

@mcp.tool()
def paper_citation_stats(paper_key: str) -> str:
    """Citation-layer stats for a single paper:

    - external citation_count (from Semantic Scholar or OpenAlex,
      populated by `kb-citations refresh-counts` /
      `refresh_citation_counts` tool)
    - in-degree: how many LOCAL papers cite this one
    - out-degree: how many LOCAL papers this one cites
    - dangling out-refs: how many DOIs this paper cites that aren't
      in your library (read from the citation cache)

    Use when ranking a paper's importance (high in-degree = local
    foundation; high citation_count = field foundation), or when
    picking what to read next from one paper's references.

    Args:
        paper_key: Zotero key (8-char alphanum) of the paper.
    """
    _lazy_reindex()
    return paper_citation_stats_impl(_store_obj(), _kb_root(), paper_key)


@mcp.tool()
def top_cited_papers(
    limit: int = 20,
    sort_by: str = "citation_count",
    min_year: int | None = None,
) -> str:
    """Rank papers in the library by citation metric.

    Two sort modes:
    - "citation_count" (default): external citation count from
      Semantic Scholar / OpenAlex. Identifies field-level foundation
      papers.
    - "in_degree": how many papers IN YOUR LIBRARY cite this one.
      Identifies local-subgraph foundations — the papers that your
      corpus is actually built around, which may differ from global
      field hits.

    Used by agents to suggest canonical reading order, to find what
    the library's center of mass is, or to prioritize notes /
    summaries on high-impact papers.

    Args:
        limit: max rows (default 20, cap 100).
        sort_by: "citation_count" | "in_degree".
        min_year: optional lower-bound year filter (e.g. 2015 to
            exclude old classics).
    """
    _lazy_reindex()
    return top_cited_papers_impl(
        _store_obj(), limit=limit, sort_by=sort_by, min_year=min_year,
    )


@mcp.tool()
def dangling_references(
    limit: int = 50,
    min_cited_by: int = 2,
) -> str:
    """Reading list: DOIs cited by local papers but NOT in the library.

    Aggregates every `references[].doi` in the citation cache whose
    DOI isn't in the `papers` table, sorted by "how many local
    papers cite it". A DOI cited by 5 local papers is almost
    certainly a foundational piece you should import into Zotero.

    Does NOT hit any external API; purely reads the citation cache
    written by `kb-citations fetch`. If cache is empty, tells you so.

    Args:
        limit: max DOIs to return (default 50, cap 200).
        min_cited_by: only show DOIs cited by at least N local
            papers (default 2 — filters noise from one-off
            references).
    """
    _lazy_reindex()
    return dangling_references_impl(
        _store_obj(), _kb_root(),
        limit=limit, min_cited_by=min_cited_by,
    )


# ----------------------------------------------------------------------
# Citation layer — trigger tools (Phase 4)
#
# These hit external APIs. They can take significant wall time (fetch
# is ~20min for 1200 papers) and cost quota. Prefer calling with
# `paper_keys` to scope to a handful after importing new papers —
# full-library runs should usually be done from CLI by the human.
# ----------------------------------------------------------------------

@mcp.tool()
def fetch_citations(
    paper_keys: list[str] | None = None,
    provider: str | None = None,
    with_incoming: bool = False,
    max_api_calls: int | None = None,
) -> str:
    """Pull reference lists from Semantic Scholar or OpenAlex into
    the local citation cache. Subset-aware.

    Use this AFTER importing a batch of new papers to populate their
    citation data. For a full-library refresh prefer the CLI
    (`kb-citations fetch`) since runs can take 20+ minutes.

    After fetching, call `link_citations` to push the edges into
    kb-mcp's links table (so backlinks/trace_links see them). Then
    optionally `refresh_citation_counts` to populate
    `papers.citation_count`.

    Args:
        paper_keys: if given, only fetch these papers. None = all
            papers in the library that have a DOI. Strongly prefer
            a subset when the agent invokes this (respect quota).
        provider: "semantic_scholar" (default) | "openalex". If
            Semantic Scholar returns 401/403, switch to openalex.
        with_incoming: also pull who-cites-this (doubles API cost).
            Usually False.
        max_api_calls: hard cap on provider calls for this run.
            Recommend 50-100 when invoking from an agent.
    """
    return _cmd_fetch_citations_impl(
        _kb_root(),
        paper_keys=paper_keys,
        provider=provider,
        with_incoming=with_incoming,
        max_api_calls=max_api_calls,
    )


@mcp.tool()
def link_citations() -> str:
    """Push cached citation edges into kb-mcp's links table.

    Fast (seconds) — purely local SQLite operations. Rewrites all
    `origin='citation'` edges atomically (INSERT OR IGNORE, so
    provider duplicates don't roll back the transaction).

    Run after `fetch_citations` to make the new edges visible to
    `backlinks`, `trace_links`, and `paper_citation_stats`.
    """
    return _cmd_link_citations_impl(_kb_root())


@mcp.tool()
def refresh_citation_counts(
    paper_keys: list[str] | None = None,
    provider: str | None = None,
    max_api_calls: int | None = None,
) -> str:
    """Update `papers.citation_count` via 1 provider call per paper.

    Citation counts grow over time — a paper cited 50 times last year
    is 80 now. Run periodically for the whole library, or scoped to
    a handful of papers an agent is currently reasoning about.

    Args:
        paper_keys: if given, only refresh these (otherwise all
            papers with a DOI). Scope the call when agents invoke
            this — a 1200-paper sweep belongs on CLI.
        provider: "semantic_scholar" (default) | "openalex".
        max_api_calls: hard cap. Recommend 50-100 from an agent.
    """
    return _cmd_refresh_counts_impl(
        _kb_root(),
        paper_keys=paper_keys,
        provider=provider,
        max_api_calls=max_api_calls,
    )


@mcp.tool()
def similar_paper_prior(paper_key: str, limit: int = 10) -> str:
    """Top-K most-similar papers from the saved similarity prior.

    Reads `ee-kb/.kb-mcp/similarity-prior.json` — a model-agnostic
    snapshot of nearest-neighbor relations extracted from the
    current embedding model via `kb-mcp similarity-prior-save`.

    Differs from `related_papers` (which queries the live vector
    index): this reads a frozen snapshot, useful for:
    - comparing what the OLD model thought was similar (after a
      provider switch, before you've re-run save)
    - getting similar-paper info without loading sqlite-vec

    If the prior file is missing, returns a hint to run
    `kb-mcp similarity-prior-save`. Not a replacement for
    `related_papers`; use that for live queries.

    Args:
        paper_key: Zotero key (8-char alphanum).
        limit: max neighbors to return (default 10, cap 50).
    """
    from .tools.similarity_prior import read_prior
    limit = max(1, min(50, limit))
    prior = read_prior(_kb_root())
    if prior is None:
        return (
            "no similarity prior saved. run `kb-mcp similarity-prior-save` "
            "first, then this tool returns pre-computed neighbors."
        )
    nbrs = prior.get("neighbors", {}).get(paper_key)
    if not nbrs:
        return f"(no neighbors recorded for {paper_key} in the prior)"
    info = prior["extracted_from"]
    lines = [
        f"prior for {paper_key} (from {info.get('model','?')}, "
        f"captured {prior.get('extracted_at','?')}):",
    ]
    for n in nbrs[:limit]:
        lines.append(
            f"  #{n['rank']:>2}  {n['key']}  distance={n['distance']}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Write tools (require kb_write)
# ----------------------------------------------------------------------

if _WRITE_OK:

    @mcp.tool()
    def create_thought(
        title: str,
        body: str,
        slug: str | None = None,
        refs: list[str] | None = None,
        tags: list[str] | None = None,
        git_commit: bool = True,
    ) -> str:
        """Create a new thought md (`thoughts/YYYY-MM-DD-<slug>.md`).

        Use for cross-paper insights, half-formed ideas, reading
        notes that span more than one paper. All fields except
        title/body are optional.

        The filename uses today's date + slugified title unless you
        pass an explicit slug (which must match the YYYY-MM-DD-name
        pattern). All writes go through kb-write's validation +
        atomic replace + git commit pipeline.

        Args:
            title: required, human-readable title.
            body: required, markdown body.
            slug: optional; auto-generated from title + today if absent.
            refs: kb_refs entries (e.g. ["papers/ABCD1234"]).
            tags: kb_tags entries.
            git_commit: commit to git after write (default True).
        """
        return write_ops.create_thought_impl(
            _kb_root(), title, body,
            slug=slug, refs=refs or [], tags=tags or [],
            git_commit=git_commit,
        )

    @mcp.tool()
    def update_thought(
        target: str,
        expected_mtime: float,
        body: str | None = None,
        title: str | None = None,
        refs: list[str] | None = None,
        tags: list[str] | None = None,
        refs_mode: str = "replace",
        tags_mode: str = "replace",
        git_commit: bool = True,
    ) -> str:
        """Update an existing thought. ONLY pass fields you want to
        change; omitted fields are preserved.

        Use read_md or find_paper_by_key first to get the current
        mtime; pass that as expected_mtime. If the file changed
        between your read and this write, you'll get a clear
        conflict error — re-read and retry.

        Args:
            target: "thoughts/SLUG" or bare SLUG.
            expected_mtime: mtime from your last read (required).
            body: new body (optional; omit to keep existing).
            title: new frontmatter title.
            refs, tags: lists; apply per refs_mode/tags_mode.
            refs_mode, tags_mode: "replace" (default) | "add" | "remove".
        """
        return write_ops.update_thought_impl(
            _kb_root(), target, expected_mtime,
            body=body, title=title,
            refs=refs, tags=tags,
            refs_mode=refs_mode, tags_mode=tags_mode,
            git_commit=git_commit,
        )

    @mcp.tool()
    def create_topic(
        slug: str,
        title: str,
        body: str,
        refs: list[str] | None = None,
        tags: list[str] | None = None,
        git_commit: bool = True,
    ) -> str:
        """Create a new topic page (`topics/<slug>.md`).

        Topics are organizational headers that group related papers
        and thoughts. Use when a cluster of references deserves its
        own named collection (e.g. "gfm-stability", "port-hamiltonian-
        converters").

        slug is required (unlike thoughts, topics are named
        deliberately) and must be kebab-case. Hierarchical slugs
        like "attention/overview" are allowed — they create
        nested files.
        """
        return write_ops.create_topic_impl(
            _kb_root(), slug, title, body,
            refs=refs or [], tags=tags or [],
            git_commit=git_commit,
        )

    @mcp.tool()
    def update_topic(
        target: str,
        expected_mtime: float,
        body: str | None = None,
        title: str | None = None,
        refs: list[str] | None = None,
        tags: list[str] | None = None,
        refs_mode: str = "replace",
        tags_mode: str = "replace",
        git_commit: bool = True,
    ) -> str:
        """Update an existing topic page. Same semantics as
        update_thought — pass expected_mtime from your last read;
        omit any field to leave it unchanged."""
        return write_ops.update_topic_impl(
            _kb_root(), target, expected_mtime,
            body=body, title=title,
            refs=refs, tags=tags,
            refs_mode=refs_mode, tags_mode=tags_mode,
            git_commit=git_commit,
        )

    @mcp.tool()
    def append_ai_zone(
        target: str,
        expected_mtime: float,
        title: str,
        body: str,
        entry_date: str | None = None,
        git_commit: bool = True,
    ) -> str:
        """Append a dated entry to the AI zone of a paper/note md.

        v26 behaviour: the AI zone accumulates entries over time;
        each call to this tool INSERTS a new entry at the top of
        the zone (newest first) while leaving older entries
        verbatim — append-only. Entries use
        `### YYYY-MM-DD — <title>` headings.

        Everything OUTSIDE the zone is preserved verbatim (paper
        abstract, Zotero notes, AI summary from kb-fulltext region).

        Use this for: reading notes on the paper, connections to
        other work, questions raised on re-read. NOT for fixing
        errors in the AI summary — for that, use `re_summarize`.

        Args:
            target: "papers/ABCD1234" (paper, or book chapter like
                    papers/BOOKKEY-ch03), "topics/standalone-note/KEY".
            expected_mtime: mtime from your last read_md call.
            title: one-line heading for this entry.
            body: entry content; may contain Markdown.
            entry_date: optional ISO date (YYYY-MM-DD); defaults to
                        today. Use for test / reproducible backfill.
        """
        return write_ops.append_ai_zone_impl(
            _kb_root(), target, expected_mtime,
            title=title, body=body,
            entry_date=entry_date,
            git_commit=git_commit,
        )

    @mcp.tool()
    def read_ai_zone(target: str) -> str:
        """Read the current AI zone content + mtime. Use this as the
        read half of a read-modify-append cycle for append_ai_zone:
        read to see what's already there (so you don't duplicate a
        reading-note), then pass the mtime back to append_ai_zone.

        v26: the returned body may contain multiple
        `### YYYY-MM-DD — <title>` entries, newest first.
        """
        return write_ops.read_ai_zone_impl(_kb_root(), target)

    @mcp.tool()
    def add_kb_tag(
        target: str, tag: str,
        expected_mtime: float | None = None,
        git_commit: bool = True,
    ) -> str:
        """Append `tag` to the kb_tags frontmatter of any md
        (paper, note, topic, thought). Duplicate tags are silently
        ignored. Does NOT modify the body, the AI zone, or
        zotero_tags. mtime guard optional (tag ops are low-stakes)."""
        return write_ops.add_kb_tag_impl(
            _kb_root(), target, tag,
            expected_mtime=expected_mtime, git_commit=git_commit,
        )

    @mcp.tool()
    def remove_kb_tag(
        target: str, tag: str,
        expected_mtime: float | None = None,
        git_commit: bool = True,
    ) -> str:
        """Remove `tag` from kb_tags. No-op if absent."""
        return write_ops.remove_kb_tag_impl(
            _kb_root(), target, tag,
            expected_mtime=expected_mtime, git_commit=git_commit,
        )

    @mcp.tool()
    def add_kb_ref(
        target: str, ref: str,
        expected_mtime: float | None = None,
        git_commit: bool = True,
    ) -> str:
        """Append `ref` to kb_refs of any md. `ref` must be a
        well-formed path like "papers/ABCD1234" or "topics/xyz".
        The link graph will pick it up on next index."""
        return write_ops.add_kb_ref_impl(
            _kb_root(), target, ref,
            expected_mtime=expected_mtime, git_commit=git_commit,
        )

    @mcp.tool()
    def remove_kb_ref(
        target: str, ref: str,
        expected_mtime: float | None = None,
        git_commit: bool = True,
    ) -> str:
        """Remove `ref` from kb_refs. No-op if absent."""
        return write_ops.remove_kb_ref_impl(
            _kb_root(), target, ref,
            expected_mtime=expected_mtime, git_commit=git_commit,
        )

    @mcp.tool()
    def create_preference(
        slug: str, body: str,
        scope: str = "global", priority: int = 50,
        title: str | None = None,
        git_commit: bool = True,
    ) -> str:
        """Create a new persistent user preference under
        `.agent-prefs/<slug>.md`. Use when the user says "remember
        to ...", "from now on ...", or "I prefer ...".

        Prefs apply across ALL future agent sessions (Claude Code,
        opencode, MCP clients, etc.) — every agent working in this
        KB will read them at session start. Think twice before adding
        — a pref is a cross-session contract.

        Args:
            slug: kebab-case, no '/'. E.g. "writing-style", "ai-summary".
            body: markdown explaining the preference.
            scope: tag for when this applies. Common scopes: "global",
                   "writing", "research", "ai-summary", "code".
            priority: 0-100; higher wins on conflict. Default 50.
            title: human-readable title (default: "<slug> preferences").
        """
        return write_ops.create_preference_impl(
            _kb_root(), slug, body,
            scope=scope, priority=priority, title=title,
            git_commit=git_commit,
        )

    @mcp.tool()
    def update_preference(
        slug: str, expected_mtime: float,
        body: str | None = None,
        scope: str | None = None,
        priority: int | None = None,
        title: str | None = None,
        git_commit: bool = True,
    ) -> str:
        """Update an existing preference file. Pass only the fields
        you want changed. last_updated is bumped automatically."""
        return write_ops.update_preference_impl(
            _kb_root(), slug, expected_mtime,
            body=body, scope=scope, priority=priority, title=title,
            git_commit=git_commit,
        )

    @mcp.tool()
    def delete_node(
        target: str, confirm: bool = False,
        git_commit: bool = True,
    ) -> str:
        """Delete a thought, topic, or preference file.

        Papers and notes CANNOT be deleted here — they're managed by
        kb-importer from Zotero. To remove a paper, remove it from
        Zotero first, then run kb-importer's orphan cleanup.

        This operation is destructive. You MUST pass confirm=True
        (not a default) to actually delete. Use git to recover if
        you regret it.

        Args:
            target: E.g. "thoughts/2026-04-22-x", "topics/foo",
                    ".agent-prefs/writing-style".
            confirm: must be True. Guard against accidental deletion.
        """
        return write_ops.delete_node_impl(
            _kb_root(), target, confirm=confirm, git_commit=git_commit,
        )

    @mcp.tool()
    def kb_doctor(fix: bool = False) -> str:
        """Scan the KB for rule violations and (optionally) repair
        them. Use when something looks off — missing AI zone
        markers, slug conventions broken, dangling kb_refs, etc.

        With fix=True, safely repairable issues are fixed (missing
        AI zone markers appended to empty zones, missing scaffold
        files recreated). Risky repairs (ambiguous marker state)
        are never auto-applied.

        Run without fix first to review; run with fix=True to apply.
        Fixes are NOT auto-committed to git — review `git diff` and
        commit manually."""
        return write_ops.doctor_impl(_kb_root(), fix=fix)


@mcp.tool()
def index_status() -> str:
    """Report the state of the kb-mcp projection DB.

    Use when:
    - Something expected doesn't show up in search (maybe index is stale).
    - Curious how many papers/notes/summaries exist.
    - Debugging: check stale/missing/orphan counts.

    Output includes row counts per table, AI summary coverage, and
    staleness detection (md mtime ahead of DB).
    """
    # Don't reindex before reporting — we want to see the CURRENT
    # (possibly stale) state.
    return index_status_impl(_store_obj(), _kb_root())


@mcp.tool()
def kb_report(
    days: int = 30,
    sections: str | None = None,
    include_normal: bool = False,
) -> str:
    """Periodic operational digest of kb-importer / kb-write events.

    Default runs five sections:
    - `ops`: "library-level operations" in window — one entry per
      command invocation of `kb-importer import`, `kb-citations
      fetch/link/refresh-counts`, or `kb-mcp reindex/snapshot`.
      Merges import_run + citations_run + index_op events into one
      block with per-subcommand counts. This is the "what big
      things did I do this month" answer.
    - `skip`: fulltext processing failures from events.jsonl
      (quota / PDF / LLM errors, grouped by category).
    - `re_read`: `kb-write re-read` batch outcomes over the window
      (selector usage + success/skip counts).
    - `re_summarize`: single-paper `kb-write re-summarize` runs
      over the window.
    - `orphans`: LIVE Zotero scan — md files / attachment dirs
      whose Zotero counterpart is gone. Not historical; reflects
      state at the moment the report is generated.

    Use when:
    - You want a "what has the library been doing" summary.
    - You want to know which papers failed fulltext over the past
      week/month, grouped by cause.
    - You suspect KB ↔ Zotero drift.
    - Periodic maintenance check.

    Args:
        days: Window size in days (default 30). Applies to ops /
              skip / re_read / re_summarize sections; ignored by
              orphans (always "now").
        sections: Comma-separated section names. Default runs all
                  registered sections: ops, skip, re_read,
                  re_summarize, orphans.
        include_normal: If True, include "normal" skip categories
                  like `already_processed` in the skip section.
                  Off by default — those aren't problems.

    Output is markdown.
    """
    from .tools.report import generate_report
    sec_list = None
    if sections:
        sec_list = [s.strip() for s in sections.split(",") if s.strip()]
    return generate_report(
        _kb_root(),
        days=days, sections=sec_list,
        include_normal=include_normal,
    )


# ---------------------------------------------------------------------
# Citation trigger helpers (lazy-import kb_citations; run sync in MCP).
# ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    global _cfg, _store, _embedder

    args = build_parser().parse_args(argv)

    try:
        _cfg = load_config(config_path=args.config, kb_root=args.kb_root)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    level = "debug" if args.verbose else _cfg.log_level
    _setup_logging(level)

    if not _cfg.kb_root.exists():
        log.error("KB root does not exist: %s", _cfg.kb_root)
        return 2

    # ------------------------------------------------------------------
    # `snapshot` preprocessing: runs BEFORE Store construction.
    #
    # Why early: `ensure_schema()` creates an empty index.sqlite on
    # first run. If we let that happen on a fresh machine before
    # `snapshot import` could run, the import path would then see
    # "index.sqlite already exists" and refuse. User ends up having
    # to pass --force on what should be a clean restore. Pre-empting
    # here keeps "import into a fresh KB" the zero-flag happy path.
    # ------------------------------------------------------------------
    if args.command == "snapshot":
        from .tools.snapshot import export_snapshot, import_snapshot
        action = getattr(args, "snapshot_action", None)
        if action == "export":
            # export requires a DB to exist — which means ensure_schema
            # must have run at some point. But we don't need to run it
            # NOW; if user never ran kb-mcp index, export correctly
            # errors with "no index.sqlite".
            try:
                result = export_snapshot(_cfg.kb_root, args.path)
            except FileNotFoundError as e:
                print(f"error: {e}", file=sys.stderr)
                _emit_index_op_event(
                    _cfg.kb_root, subcommand="snapshot-export", rc=2,
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
                return 2
            except (PermissionError, OSError) as e:
                # Disk full, permission denied, bad target dir, etc.
                # Give a clean error instead of a raw traceback so
                # systemd journal shows something actionable.
                print(
                    f"error: could not write snapshot ({type(e).__name__}): {e}",
                    file=sys.stderr,
                )
                _emit_index_op_event(
                    _cfg.kb_root, subcommand="snapshot-export", rc=2,
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
                return 2
            mb = result["size_bytes"] / (1024 * 1024)
            print(f"wrote snapshot: {result['path']} ({mb:.1f} MB)")
            print("included:")
            for item in result["includes"]:
                print(f"  {item}")
            _emit_index_op_event(
                _cfg.kb_root, subcommand="snapshot-export", rc=0,
                extra={
                    "path":       str(result["path"]),
                    "size_bytes": result["size_bytes"],
                    "includes":   result["includes"],
                },
            )
            return 0

        if action == "import":
            try:
                result = import_snapshot(
                    _cfg.kb_root, args.path, force=args.force,
                )
            except (FileNotFoundError, FileExistsError) as e:
                print(f"error: {e}", file=sys.stderr)
                _emit_index_op_event(
                    _cfg.kb_root, subcommand="snapshot-import", rc=2,
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
                return 2
            except tarfile.TarError as e:
                print(
                    f"error: snapshot tar is corrupt or not a valid "
                    f"kb-mcp snapshot: {e}",
                    file=sys.stderr,
                )
                _emit_index_op_event(
                    _cfg.kb_root, subcommand="snapshot-import", rc=2,
                    extra={"error": f"tar: {e}"},
                )
                return 2
            except (PermissionError, OSError) as e:
                print(
                    f"error: could not restore snapshot "
                    f"({type(e).__name__}): {e}",
                    file=sys.stderr,
                )
                _emit_index_op_event(
                    _cfg.kb_root, subcommand="snapshot-import", rc=2,
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
                return 2
            print(f"restored into {_cfg.kb_root}:")
            for item in result["restored"]:
                print(f"  {item}")
            print(
                "\nrun `kb-mcp index-status` to verify, then "
                "`kb-mcp serve` as usual."
            )
            _emit_index_op_event(
                _cfg.kb_root, subcommand="snapshot-import", rc=0,
                extra={
                    "restored": result["restored"],
                    "force":    bool(args.force),
                },
            )
            return 0

        print("snapshot requires an action: export|import", file=sys.stderr)
        return 2
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # `reindex` preprocessing: apply CLI overrides to Config and wipe
    # the sqlite file if --force, BEFORE constructing Store/embedder.
    # After this block, control flow falls through to the normal
    # `index` path, which builds everything fresh with the new config.
    # ------------------------------------------------------------------
    if args.command == "reindex":
        if not args.force:
            print(
                "reindex is destructive (drops the projection DB). "
                "Re-run with --force to confirm.",
                file=sys.stderr,
            )
            return 2

        # Apply provider/model/dim overrides to Config.
        if args.provider:
            _cfg.embedding_provider = args.provider
            # Default model if user didn't explicitly set one.
            if not args.model:
                from .config import _resolve_embedding_model
                _cfg.embedding_model = _resolve_embedding_model(
                    args.provider, None,
                )
        if args.model:
            _cfg.embedding_model = args.model
        if args.dim:
            _cfg.embedding_dim = args.dim

        # Wipe the projection DB. Also drop sidecars in case user
        # was previously on WAL.
        db_path = default_db_path(_cfg.kb_root)
        removed = []
        for p in [db_path,
                  db_path.with_suffix(db_path.suffix + "-wal"),
                  db_path.with_suffix(db_path.suffix + "-shm"),
                  db_path.with_suffix(db_path.suffix + "-journal")]:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        print(
            f"reindex: wiped {', '.join(removed) or '(nothing to wipe)'}. "
            f"Rebuilding with provider={_cfg.embedding_provider}, "
            f"model={_cfg.embedding_model}, dim={_cfg.embedding_dim}.",
            file=sys.stderr,
        )
        # Fall through to `index` logic.
        args.command = "index"
        args.full = True
        # Mark this invocation so the `index` branch below knows
        # this run started as `reindex --force` (deserves an
        # INDEX_OP event; ordinary `index` does not — it runs too
        # often).
        args._from_reindex = True
    # ------------------------------------------------------------------

    # Open/create the projection DB. Both `serve` and `index` need it.
    # vec_dim: config's embedding_dim overrides the default 1536. This
    # MUST match the embedding provider's output — text-embedding-3-small
    # and gemini-embedding-001 default to 1536, but text-embedding-3-large
    # at full size is 3072. Mismatch → sqlite-vec insert errors.
    _store = Store(
        default_db_path(_cfg.kb_root),
        journal_mode=_cfg.journal_mode,
        vec_dim=_cfg.embedding_dim or 1536,
    )
    _store.ensure_schema()

    # Phase 2b: try to construct an embedding provider. None if config
    # disables it or the API key is missing — degraded mode, but
    # everything else still works.
    _embedder = build_from_config(_cfg)
    if _embedder is not None:
        log.info(
            "Embedding provider ready: %s (dim=%d)",
            _embedder.model_name, _embedder.dim,
        )
    else:
        log.info("Embedding provider disabled; vector search unavailable.")

    command = args.command or "serve"

    if command == "index":
        # Subset-mode args (may both be None for full index).
        only_keys = None
        if getattr(args, "only_key", None):
            only_keys = {k.strip() for k in args.only_key.split(",") if k.strip()}
        path_glob = getattr(args, "path_glob", None)
        if only_keys or path_glob:
            parts = []
            if only_keys:
                parts.append(f"{len(only_keys)} key(s)")
            if path_glob:
                parts.append(f"glob={path_glob!r}")
            print(f"Subset index: {' + '.join(parts)} "
                  "(orphan removal skipped)", file=sys.stderr)

        report = Indexer(
            _cfg.kb_root, _store,
            embedding_provider=_embedder,
            embedding_batch_size=_cfg.embedding_batch_size,
            only_keys=only_keys,
            path_glob=path_glob,
        ).index_all()
        print(f"Indexed: new={report.new}, updated={report.updated}, "
              f"unchanged={report.unchanged}, removed={report.removed}")
        if _embedder is not None or report.embedded_papers or report.embed_skipped:
            print(
                f"Embedding: {report.embedded_papers} paper(s), "
                f"{report.embedded_chunks} chunks, "
                f"{report.embed_api_calls} API call(s), "
                f"{report.embed_tokens} tokens"
                + (f", {report.embed_failed} failed" if report.embed_failed else "")
                + (f", {report.embed_skipped} skipped" if report.embed_skipped else "")
            )
        if report.links_written or report.links_dangling:
            print(
                f"Links: {report.links_written} edge(s)"
                + (f", {report.links_dangling} dangling" if report.links_dangling else "")
            )
        if report.errors:
            print(f"Errors ({len(report.errors)}):")
            for path, msg in report.errors[:10]:
                print(f"  {path}: {msg}")
            if len(report.errors) > 10:
                print(f"  ... +{len(report.errors) - 10} more")
        rc = 0 if not report.errors else 1
        # v26.x: emit an INDEX_OP event only when this was a full
        # rebuild (`kb-mcp reindex --force`). Ordinary `kb-mcp index`
        # runs every time an MCP tool is called (lazy reindex) — it
        # would flood events.jsonl with noise. Full reindex is a
        # deliberate operator action and deserves a landmark.
        if getattr(args, "_from_reindex", False):
            _emit_index_op_event(
                _cfg.kb_root, subcommand="reindex", rc=rc,
                extra={
                    "provider": _cfg.embedding_provider,
                    "model":    _cfg.embedding_model,
                    "dim":      _cfg.embedding_dim,
                    "errors":   len(report.errors),
                },
            )
        return rc

    if command == "index-status":
        print(index_status_impl(
            _store, _cfg.kb_root,
            deep=getattr(args, "deep", False),
        ))
        return 0

    if command == "report":
        from .tools.report import generate_report
        sections = None
        if args.sections:
            sections = [s.strip() for s in args.sections.split(",") if s.strip()]
        since_dt = None
        if args.since:
            # Accept bare date or full ISO. datetime.fromisoformat
            # handles both in Python 3.11+. Attach UTC if naive.
            try:
                since_dt = datetime.fromisoformat(args.since)
            except ValueError:
                print(
                    f"error: --since {args.since!r} is not a valid "
                    "ISO date/datetime (expected e.g. 2026-04-01 or "
                    "2026-04-01T00:00:00Z).",
                    file=sys.stderr,
                )
                return 2
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        text = generate_report(
            _cfg.kb_root,
            days=args.days,
            since=since_dt,
            sections=sections,
            include_normal=args.include_normal,
        )
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(f"wrote report: {args.out}")
        else:
            print(text)
        return 0

    if command == "similarity-prior-save":
        from .tools.similarity_prior import (
            extract_similarity_prior, write_prior,
        )
        try:
            prior = extract_similarity_prior(
                _store, _cfg.kb_root, top_k=args.top_k,
            )
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        out = write_prior(_cfg.kb_root, prior)
        info = prior["extracted_from"]
        print(
            f"wrote {out.relative_to(_cfg.kb_root)}\n"
            f"  paper_count: {info['paper_count']}\n"
            f"  model:       {info['model']}\n"
            f"  dim:         {info['dim']}\n"
            f"  top_k:       {prior['top_k']}"
        )
        return 0

    if command == "similarity-prior-compare":
        from .tools.similarity_prior import (
            extract_similarity_prior, read_prior, compare_priors,
        )
        old = read_prior(_cfg.kb_root)
        if old is None:
            print(
                "error: no saved prior at "
                f"{_cfg.kb_root}/.kb-mcp/similarity-prior.json\n"
                "run `kb-mcp similarity-prior-save` BEFORE changing "
                "the embedding model so you have a baseline to "
                "compare against.",
                file=sys.stderr,
            )
            return 2
        try:
            new = extract_similarity_prior(
                _store, _cfg.kb_root,
                top_k=old.get("top_k", 20),
            )
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        cmp = compare_priors(old, new, at_k=args.at_k)
        print(f"similarity-prior comparison (at_k={cmp['at_k']}):")
        print(f"  old model: {cmp['old_model']}")
        print(f"  new model: {cmp['new_model']}")
        print(f"  papers compared:   {cmp['paper_count']}")
        print(f"  mean Jaccard:      {cmp['mean_jaccard']}")
        if cmp['mean_jaccard'] < 0.3:
            print("  ⚠ low overlap — new model disagrees strongly with old.")
        elif cmp['mean_jaccard'] < 0.5:
            print("  moderate overlap — some drift, likely OK.")
        else:
            print("  ✓ high overlap — new embedding is consistent with old.")
        if cmp["low_overlap_papers"]:
            print("  low-overlap papers (bottom 20):")
            for e in cmp["low_overlap_papers"]:
                print(f"    {e['key']}  jaccard={e['jaccard']}")
        return 0

    # Default: run MCP server.
    #
    # Graceful shutdown: on SIGTERM (systemd, container orchestrator,
    # `kill`), flush the Store and close the SQLite connection before
    # exiting. Without this, the process could die with WAL/SHM files
    # dirty, leaving a partial journal that the next `kb-mcp index`
    # run would need to recover.
    #
    # v0.27.4 handler rework: a prior version of this handler did
    # `raise KeyboardInterrupt` inside the signal handler, expecting
    # mcp.run()'s asyncio loop to catch it like it does for Ctrl-C.
    # That didn't work in practice — the MCP stdio transport blocks
    # on readline() waiting for the next JSON-RPC message from the
    # client, and Python only runs a signal handler between
    # bytecode instructions. A blocking syscall never returns to
    # bytecode, so the handler never fires. Result: `kill -TERM`
    # did nothing, and systemd fell back to SIGKILL after
    # TimeoutStopSec — exactly the problem this code was meant to
    # prevent.
    #
    # The reliable fix is to close the store inside the handler
    # (SQLite's WAL-checkpoint logic is synchronous and doesn't
    # need the Python interpreter state to be in a good spot) and
    # then call os._exit(). That bypasses atexit / GC, but the
    # important "close the DB cleanly" part already ran.
    #
    # SIGINT is also wired to the same handler: mcp.run()'s own
    # handler DOES work for it (readline returns with EINTR in
    # some Python versions) but not reliably across platforms.
    # Treating both signals uniformly is simpler and matches what
    # an operator expects from Ctrl-C.
    import os as _os
    import signal as _signal

    def _graceful_shutdown(signum, _frame):
        log.info(
            "kb-mcp: received signal %s, shutting down cleanly", signum,
        )
        try:
            if _store is not None:
                _store.close()
        except Exception:
            log.exception("error closing store during shutdown")
        # os._exit bypasses the Python-level cleanup that normal
        # exit() does — atexit callbacks, GC, stdio flush — but all
        # the state that MATTERS (SQLite WAL checkpoint in
        # _store.close() above) has already been flushed to disk.
        # Using sys.exit() would raise SystemExit, which has the
        # same "won't fire through a blocking read" problem as
        # KeyboardInterrupt.
        _os._exit(0)

    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            _signal.signal(_sig, _graceful_shutdown)
        except (ValueError, OSError):
            # signal.signal() fails when called off the main thread;
            # harmless in that case — asyncio's own signal wiring
            # remains. Don't block `serve` over a signal-wiring edge
            # case.
            pass

    log.info("kb-mcp serving; kb_root=%s, db=%s",
             _cfg.kb_root, _store.db_path)
    try:
        mcp.run()
    except KeyboardInterrupt:
        log.info("kb-mcp: exited cleanly via interrupt")
    finally:
        try:
            if _store is not None:
                _store.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
