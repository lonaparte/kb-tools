<!-- fragment: citation_workflow -->
## Citation layer — don't forget to populate it

The KB has four ways for papers to relate to each other. Three are
present by default; the fourth is easy to forget:

1. **Strong ties** — `kb_refs` a user or agent wrote into a thought
   or topic. Curated, deliberate.
2. **Semantic ties** — vector search (via `kb-mcp`). Automatic;
   covers fuzzy "papers about similar things".
3. **Wikilinks** — `[[paper-slug]]` references inside md body.
   Automatic via `kb-mcp index`.
4. **Citation edges** — "paper A's reference list mentions paper B"
   as a weak tie. These are **NOT populated by default** — they come
   from the `kb-citations` tool querying Semantic Scholar or
   OpenAlex. If you skip this step, your RAG misses a real signal:
   foundation papers (high in-degree) and bridge papers that link
   distant subfields.

### When to run it

Run the three-command sequence after any of:

- First-time KB setup (after `kb-importer` sync)
- After importing a significant batch of new papers (~50+)
- Every 1–3 months as a refresh, since citation counts grow over
  time

```bash
kb-citations fetch               # pull reference lists (~20 min for 1200 papers)
kb-citations link                # write edges into kb-mcp's links table
kb-citations refresh-counts      # update each paper's citation_count column
```

`fetch` caches per-paper JSON under `ee-kb/.kb-mcp/citations/`, so
a re-run only hits papers whose cache is older than
`freshness_days` (default 30). `link` is idempotent — it wipes and
rewrites the `origin='citation'` edges in one transaction.
`refresh-counts` is an N-GET-per-paper sweep that populates
`papers.citation_count` / `citation_count_source` /
`citation_count_updated_at`.

### Why citation_count matters for retrieval

A paper with thousands of citations is qualitatively different
from one with zero, even if both are semantically on-topic. When
ranking `search_hybrid` results, prefer higher-citation papers for
"give me an overview of X" queries and lower-citation papers for
"what's new / what are people still debating."

`citation_count` is just a column in the `papers` table — agents
can sort by it in any SQL query against the projection DB.

### Provider choice

- **Semantic Scholar** is the default. Needs no key for low volume.
  With a `SEMANTIC_SCHOLAR_API_KEY` in `~/.bashrc` you get 1 req/s
  sustained.
- **OpenAlex** is the fallback. Needs a contact email (not a
  secret):
  ```bash
  kb-citations --provider openalex --mailto you@example.com fetch
  ```
  or `export OPENALEX_MAILTO=you@example.com`.

If Semantic Scholar returns 403 (key revoked / quota exhausted /
invalid), kb-citations prints a suggestion to switch. Don't
interpret 403 as "this paper doesn't exist" — switch provider.

### Troubleshooting

- `kb-citations status` — shows how many papers have cached data
  and how many references/citations were collected per provider.
- `kb-citations refs <paper_key>` / `kb-citations cites <paper_key>`
  — dumps what's in cache for one paper.
- Link written but MCP's `backlinks` still empty? Run `kb-mcp
  index` — the link graph is part of the projection DB, and
  kb-mcp caches query results.

### Budget control

`kb-citations fetch --max-api-calls 500` caps the total number of
provider calls, useful when:
- A key is flaky and you don't want to burn the daily quota on
  partial runs
- You just want to sample 500 papers to test the pipeline
- You're mid-run and want to set a finite stopping point
