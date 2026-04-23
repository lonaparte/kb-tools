# kb-citations

Fetch paper-to-paper citation edges from public APIs (Semantic
Scholar or OpenAlex) and inject them into the ee-kb link graph as
`origin="citation"` edges.

## Why

Your KB already has three ways for papers to be linked:

- **Strong ties**: you write `kb_refs: [papers/X]` in a thought or
  topic — deliberate, curated
- **Semantic ties**: kb-mcp's vector search — automatic, fuzzy
- **Weak ties**: "paper A's reference list mentions paper B" —
  this is what kb-citations adds

The weak-tie layer matters because:
- **Foundation papers** surface as high in-degree nodes (every
  paper in a subfield cites them)
- **Bridge papers** connect distant topics (high betweenness)
- **Reading gaps**: you love 10 papers; they all cite X; X isn't
  in your library — go grab X from Zotero
- **Graph-augmented retrieval**: when kb-mcp retrieves paper A, it
  can also surface papers A cites (context deepening)

Importantly: the *existing* `backlinks` and `trace_links` MCP tools
automatically pick up citation edges — no kb-mcp changes needed on
the query side. `kb-citations link` only writes to the `links`
table.

## Install

```bash
cd kb_citations
pip install -e .
# Or with the [link] extra to ensure kb_mcp is available:
pip install -e "./kb_citations[link]"
```

Deps: `httpx`, `python-frontmatter`, `PyYAML`. kb_mcp is optional
(soft import); without it, `link` falls back to writing a JSONL file
instead of updating the DB.

## Workflow

```bash
export KB_ROOT=~/code/ee-kb

# 1) Fetch from Semantic Scholar (default). Free; ~20 min for 1200 papers.
#    Recommended: get a free API key for higher rate limits.
export SEMANTIC_SCHOLAR_API_KEY=xxxxx       # optional but recommended
kb-citations fetch                           # caches to .kb-mcp/citations/by-paper/

# Alternative: OpenAlex (requires contact email for polite pool)
export OPENALEX_MAILTO=you@example.com
kb-citations --provider openalex fetch

# 2) See what we got
kb-citations status

# 3) Push edges into kb-mcp's links table
kb-citations link

# 4) Now every kb-mcp backlinks / trace_links query sees citation edges.

# Per-paper inspection:
kb-citations refs ABCD1234                  # what ABCD1234 cites (cached)
kb-citations cites ABCD1234                 # who cites ABCD1234 (cached;
                                            #  needs --with-citations on fetch)

# 5) Periodically — refresh citation counts.
#    Cheaper than `fetch`: one provider call per paper, no reference walk.
#    Writes papers.citation_count + source + timestamp in the kb-mcp DB.
#    Citation counts grow over time; monthly cron is a reasonable cadence.
kb-citations refresh-counts
kb-citations refresh-counts --only-key ABCD1234,EFGH5678   # subset
kb-citations refresh-counts --max-api-calls 200            # cap

# 6) Find high-value dangling DOIs — papers multiple local papers cite
#    but that aren't yet in your Zotero library. Purely local (reads cache).
kb-citations suggest --min-cites 5
```

## Provider differences

| Aspect              | Semantic Scholar          | OpenAlex                    |
|---------------------|---------------------------|-----------------------------|
| Auth                | optional API key          | required `mailto` email     |
| Rate limit (anon)   | ~100 req / 5 min          | "polite pool" via mailto    |
| Rate limit (auth)   | 1 req/sec                 | 10 req/sec (polite pool)    |
| Reference coverage  | good, S2 corpus           | comprehensive, includes grey lit |
| DOI resolution      | direct `DOI:<doi>` lookup | direct `works/doi:<doi>`    |
| Title hydration     | returned in one call      | requires batch follow-up    |
| Sign-up needed      | optional                  | no                          |

For most users: **Semantic Scholar with a free API key** is the
simplest path. OpenAlex is valuable when S2 doesn't have a
particular paper (often non-CS fields).

## Cache layout

```
.kb-mcp/citations/
├── by-paper/
│   ├── ABCD1234.json          # one file per local paper
│   ├── EFGH5678.json
│   └── ...
└── citation-edges.jsonl       # (only if kb-mcp unavailable at link time)
```

Each per-paper JSON contains:

```json
{
  "paper_key": "ABCD1234",
  "doi": "10.1109/xxx",
  "provider": "semantic_scholar",
  "fetched_at": "2026-04-22T14:00:00Z",
  "references": [
    {"doi": "...", "title": "...", "year": 2020,
     "authors": ["..."], "provider_id": "...",
     "provider": "semantic_scholar"},
    ...
  ],
  "citations": [...]
}
```

Caches are independent per paper; re-running fetch with
`--freshness-days 30` skips papers recently fetched. Use
`--freshness-days 0` to force full refetch.

## Python API

```python
from kb_citations.config import CitationsContext
from kb_citations.fetcher import build_provider, fetch_all
from kb_citations.linker import link

ctx = CitationsContext(
    kb_root="/path/to/kb",
    provider="semantic_scholar",
    api_key="xxx",
    fetch_citations=False,       # only references by default
)
provider = build_provider(ctx)
try:
    report = fetch_all(ctx, provider)
    print(report)
finally:
    provider.close()

link_report = link(ctx.kb_root)
print(link_report)
```

## How edges land in kb-mcp

`kb-citations link` writes rows into the existing `links` table with
`origin="citation"`:

```sql
INSERT INTO links (src_type, src_key, dst_type, dst_key, origin)
VALUES ('paper', 'ABCD1234', 'paper', 'EFGH5678', 'citation');
```

Link-table semantics:
- Edges are **always** paper → paper (other node types can't cite)
- `src_key` is the citing paper (the one in your KB whose references
  we fetched)
- `dst_key` is the cited paper, **only if it's also in your KB**
- References pointing to papers not in your KB are **dropped** — we
  don't emit dangling edges to avoid noise. The counts appear in
  `LinkReport.edges_to_dangling` so you can see the coverage ratio.

`kb-citations link` starts by **deleting all existing citation-origin
edges** before inserting — the citation layer is replaced atomically
each run. Wikilink / kb_refs / mdlink / cite edges are never touched.

## Limitations / future

- **No citation contexts yet**: we only emit "A cites B", not "A
  cites B in the methods section". Adding this requires PDF-level
  extraction (GROBID). Reserved field `context` in the Reference
  class.
- **No incremental link**: each `link` run does a full replace of
  the citation-origin rows. Fine for 1200 papers; if you scale to
  100k, incremental would matter.
