# kb-mcp

MCP server that exposes the ee-kb knowledge base to AI clients (Claude
Desktop, Cursor, Claude Code, etc.).

**Phase 3b scope**: filesystem tools (1a) + SQLite projection with
FTS5 (2a) + vector search via `sqlite-vec` and OpenAI embeddings (2b)
+ link graph (2c) + agent preferences + full write layer via
`kb-write` (Phase 3) + periodic reporting (v26.x). Total: **36 MCP
tools** (v26 added `list_paper_parts`; v26.x added `kb_report`).

## Tools exposed

### Filesystem tools (Phase 1a — no DB needed)
| Tool | Purpose | SLA |
|------|---------|-----|
| `find_paper_by_key` | Direct lookup by Zotero key — whole-work md only | < 50 ms |
| `list_paper_parts` | List EVERY md sharing a zotero_key (v26: whole-work + `-chNN` chapter siblings) | < 50 ms |
| `list_files` | Directory listing with optional `kind` filter | fast |
| `read_md` | Read a specific md file | < 50 ms |
| `grep_md` | Literal substring search, multi-term AND | varies |

### DB-backed tools (Phase 2a — keyword + reverse index)
| Tool | Purpose | SLA |
|------|---------|-----|
| `search_papers_fts` | FTS5 keyword search, bm25 ranking | < 100 ms |
| `find_paper_by_attachment_key` | O(1) reverse lookup | < 10 ms |
| `index_status` | Diagnostics: counts + staleness | < 200 ms |
| `dangling_references` | List unresolved kb_refs / wikilinks / citations | < 100 ms |

### Vector-backed tools (Phase 2b — semantic)
| Tool | Purpose | SLA |
|------|---------|-----|
| `search_papers_hybrid` | RRF-fused keyword + vector search | ~300 ms (1 embedding call) |
| `related_papers` | kNN from anchor paper's embedding | < 100 ms |
| `similar_paper_prior` | Compare current neighbours vs saved prior (embedding-model migration) | < 100 ms |

### Link graph tools (Phase 2c)
| Tool | Purpose | SLA |
|------|---------|-----|
| `backlinks` | Who references this md | < 50 ms |
| `trace_links` | BFS from a node, depth ≤ 4 | < 200 ms |
| `search_papers_graph` | Hybrid search, then graph-expand by kb_refs / citations | < 300 ms |

The link graph has 5 edge types (`origin` column in `links`):

- `frontmatter` — `kb_refs:` field in YAML
- `wikilink` — `[[KEY]]` in md body
- `mdlink` — `[title](path.md)` in md body
- `cite` — `@citation_key` in md body
- `citation` — **paper-to-paper** from Semantic Scholar or OpenAlex
  (Phase 4). Populated by the separate `kb-citations` package, not
  `kb-mcp` itself — run `kb-citations fetch && kb-citations link`.
  After `link`, `backlinks` and `trace_links` automatically see them.

### Citation tools (Phase 4)
| Tool | Purpose | SLA |
|------|---------|-----|
| `fetch_citations` | Trigger `kb-citations fetch` from the agent side | varies (network) |
| `link_citations` | Apply cached citation edges to the `links` table | < 1 s |
| `refresh_citation_counts` | Bulk-refresh `papers.citation_count` from provider | varies (network) |
| `paper_citation_stats` | Per-paper cited-by count + in-degree + DOI | < 100 ms |
| `top_cited_papers` | Rank papers by `citation_count` or in-degree | < 100 ms |

### Reporting (v26.x)
| Tool | Purpose | SLA |
|------|---------|-----|
| `kb_report` | Periodic digest over `events.jsonl` (skips + re-reads) | < 100 ms |

### Agent preferences
| Tool | Purpose | SLA |
|------|---------|-----|
| `get_agent_preferences` | Read `.agent-prefs/*.md` at session start | < 50 ms |

### Write tools (Phase 3 — require kb-write)
| Tool | Purpose |
|------|---------|
| `create_thought` | New dated thought md |
| `update_thought` | Edit existing thought (mtime guard) |
| `create_topic` | New topic page |
| `update_topic` | Edit existing topic |
| `append_ai_zone` | **v26: replaces v25's `update_ai_zone`** — insert dated entry at top of zone, preserves older entries |
| `read_ai_zone` | Read current zone + mtime |
| `add_kb_tag` / `remove_kb_tag` | Manage kb_tags |
| `add_kb_ref` / `remove_kb_ref` | Manage kb_refs |
| `create_preference` | New `.agent-prefs/*.md` |
| `update_preference` | Edit existing preference |
| `delete_node` | Remove thought/topic/preference (requires confirm=True) |
| `kb_doctor` | Scan for rule violations, optional `--fix` |

Not exposed as MCP tools (CLI-only):
  - `re-summarize` / `re-read` — LLM-heavy; running them through MCP
    would hold an agent turn hostage for tens of seconds to minutes.
    The agent can still trigger them indirectly via the shell or
    schedule them in cron. `kb_report` surfaces their outcomes.

All write tools funnel through `kb-write`'s unified pipeline:
validation → mtime guard → atomic replace → git auto-commit →
re-index. Identical rules apply to local CLI users and MCP clients.

Each tool's docstring guides the AI toward the cheapest applicable
tool first. See `server.py`.

## Install

```bash
cd kb_mcp
pip install -e .
# For write capability, also install kb_write:
pip install -e ../kb_write
# Or in one shot:
pip install -e "./kb_mcp[write]"    # uses the [write] extra
```

Without `kb_write`, the server starts in read-only mode (write tools
disappear from the MCP tool list; a warning logs at startup).

## Embedding provider (RAG / vector index)

Three providers ship in-tree: **OpenAI** (default), **OpenRouter**,
and **Gemini**. All three output 1536-dim vectors by default, so
switching doesn't force a rebuild of the vec0 table — the stored
`embedding_model` column records which model produced each row and
`kb-mcp index` re-embeds papers whose stored model differs from the
current config.

> **Scope note.** This section is about the **RAG embedding
> pipeline only** — the short-text vectors used for semantic search
> and the link graph. The LLM that writes paper summaries during
> `kb-importer --fulltext` is a separate setup configured through
> `kb-importer.yaml` and the `--fulltext-provider` / `--fulltext-model`
> CLI flags. Changing an embedding provider here never affects
> fulltext-summary behavior.

### OpenAI (default)

```yaml
# <workspace>/.ee-kb-tools/config/kb-mcp.yaml
embeddings:
  provider: openai
  # model: text-embedding-3-small        # 1536 dim, $0.02/1M tokens
```

```bash
# in ~/.bashrc
export OPENAI_API_KEY=sk-...
```

### OpenRouter (new in 1.0)

OpenRouter (https://openrouter.ai) is an OpenAI-compatible router
to many upstream embedding providers behind one API key. By default
we route to OpenAI's text-embedding-3-small (same vectors, same
1536 dim — useful when a user already has OPENROUTER_API_KEY but
not OPENAI_API_KEY).

```yaml
embeddings:
  provider: openrouter
  # model: openai/text-embedding-3-small   # default
  #
  # Other OpenRouter embedding models (see the /models?modality=embeddings
  # catalog for current availability and pricing):
  #   openai/text-embedding-3-large        # 3072 dim — requires dim:
  #                                        #   rebuild; see note below
  #   voyage-ai/voyage-3                   # 1024 dim
  #   voyage-ai/voyage-3-large             # 1024 dim
```

```bash
# in ~/.bashrc — two env vars, embedding-specific first
export OPENROUTER_EMBEDDING_API_KEY=sk-or-...   # used by kb-mcp embedding
export OPENROUTER_API_KEY=sk-or-...             # used by kb-importer fulltext
```

**Why two env vars?** Embedding (kb-mcp) uses
`OPENROUTER_EMBEDDING_API_KEY`; fulltext summarization
(kb-importer) uses `OPENROUTER_API_KEY`. The split lets you
route the two pipelines at different OpenRouter accounts (e.g.
one billed per-project, one shared). **Single-key convenience**:
if `OPENROUTER_EMBEDDING_API_KEY` is unset but `OPENROUTER_API_KEY`
is, the embedding side silently uses the shared key — you only
need to export both variables when you actually want them
different.

The stored `papers.embedding_model` gets a `openrouter/` prefix
when you route via OpenRouter (e.g.
`openrouter/openai/text-embedding-3-small`), so switching between
direct OpenAI and OpenRouter triggers a re-embed rather than
silently reusing vectors that could diverge if either side changes.

### Gemini

```bash
pip install -e "./kb_mcp[gemini]"
```

```yaml
embeddings:
  provider: gemini
  # model: gemini-embedding-001        # 1536 dim via MRL truncation
```

```bash
# in ~/.bashrc
export GEMINI_API_KEY=...
```

## Citation edges (Phase 4)

Citation edges are populated by a **separate package**,
`kb-citations`, which fetches reference lists from Semantic Scholar
or OpenAlex and writes A→B edges into this `links` table with
`origin='citation'`. Once written, they're automatically picked up
by `backlinks`, `trace_links`, and graph-augmented retrieval.

```bash
# Fetch, link, and refresh citation counts:
kb-citations fetch              # pull reference lists (~20 min for 1200 papers)
kb-citations link               # write edges into kb-mcp links table
kb-citations refresh-counts     # populate papers.citation_count column
```

Cache at `<kb_root>/.kb-mcp/citations/<paper-key>.json`. For 1200
papers: ~20 min on first run, zero cost with the Semantic Scholar
free tier. See the `kb_citations/` package for details.

## Embedding: operational notes

If no embedding provider is configured (or the API key env var isn't
set), `kb-mcp` still works — vector-backed tools gracefully degrade
(`search_papers_hybrid` falls back to pure FTS; `related_papers`
returns a helpful error). Only the semantic layer disappears; the
filesystem / DB / link-graph tools keep working.

**Cost expectation** (OpenAI text-embedding-3-small, direct or via
OpenRouter): ~$0.08 one-time for 1200 papers with summaries.
Incremental re-index cost is trivial (embedding only changed
papers). Each `search_papers_hybrid` call embeds the query
(~10 tokens, ~$0.0000002).

**Switching to a higher-dim model** (e.g. `text-embedding-3-large`
at 3072 dim): you must delete `.kb-mcp/index.sqlite` and rebuild,
because the vec0 column dimension is compile-time fixed in the
schema. Use `kb-mcp reindex --force --provider ... --model ...
--dim <N>` to drive the rebuild.

## CLI subcommands

```bash
kb-mcp                           # run MCP stdio server (default)
kb-mcp serve                     # explicit form

# Indexing
kb-mcp index                     # refresh projection DB + embed changed papers
kb-mcp reindex --force           # drop + rebuild from scratch (e.g. to switch
                                 # embedding model: add --provider/--model/--dim)

# Diagnostics
kb-mcp index-status              # row counts + staleness report
kb-mcp index-status --deep       # additionally: PRAGMA integrity_check (slow;
                                 # catches filesystem bit-rot / torn writes)
kb-mcp report                    # periodic operational digest. Five sections:
                                 # ops / skip / re_read / re_summarize / orphans.
                                 # First four read .kb-mcp/events.jsonl; orphans
                                 # does a live Zotero scan (degrades gracefully).
                                 # --days N, --since ISO, --out file.md,
                                 # --sections ops,skip,re_read,re_summarize,orphans,
                                 # --include-normal (show already_processed).

# Backup / restore
kb-mcp snapshot export <tar>     # whole .kb-mcp/ → one tar file (DB + caches).
                                 # Uses VACUUM INTO so the output is consistent
                                 # even if `kb-mcp serve` is running.
                                 # .tar.gz suffix → gzip compression.
kb-mcp snapshot import <tar>     # restore; refuses non-empty target w/o --force.

# Embedding-model migration
kb-mcp similarity-prior-save     # snapshot top-K neighbours from current model
                                 # into .kb-mcp/similarity-prior.json
kb-mcp similarity-prior-compare  # after reindex with a new model, compare
                                 # neighbour Jaccard against the saved prior
```

`kb-mcp index` is incremental — only md files whose mtime has advanced
past the DB's recorded mtime are re-indexed. Papers whose md changed
also get re-embedded; unchanged ones skip the API call. Run it after
a big `kb-importer import` or `import-summaries` to populate both
search indices.

DB-backed tools also do a **lazy reindex** on every call (quick mtime
scan, re-index stale), so you rarely need to invoke `kb-mcp index`
manually during normal use. Caveat: lazy reindex can embed a handful
of papers per tool call, but won't batch-embed thousands — for bulk
updates use the explicit command.

### Backup tarball contents

`kb-mcp snapshot export` produces a tar containing, verbatim:

- `.kb-mcp/index.sqlite` — projection DB (papers, thoughts, topics,
  notes, links, chunks, vectors, FTS5). Written via `VACUUM INTO` so
  it's guaranteed consistent at the moment of export.
- `.kb-mcp/citations/by-paper/*.json` — kb-citations provider fetch
  cache (references + citations per paper). Rebuildable via
  `kb-citations fetch` but costs provider API calls.
- `.kb-mcp/similarity-prior.json` — saved neighbour prior (if
  `similarity-prior-save` has run). Optional.

Not in the tar (backed up separately): `ee-kb/*.md` (git), PDFs
under `zotero/storage/` (rsync), `.ee-kb-tools/config/*.yaml` (per
machine).

### Reindex decision tree

- Normal: md drifted → just run `kb-mcp index`, incremental.
- Corruption: `index-status --deep` flags it → `kb-mcp reindex --force`.
- Model change: `similarity-prior-save` first, then
  `kb-mcp reindex --force --provider X --model Y --dim Z`, then
  `similarity-prior-compare` to verify.

### Periodic report (v26.x)

`kb-mcp report` produces a markdown digest over
`<kb_root>/.kb-mcp/events.jsonl` — the append-only log where
kb-importer and kb-write record structured events (fulltext skips
by cause, re-read batch outcomes, etc.). The file is also auto-
included in `kb-mcp snapshot export`.

**Six event types in events.jsonl.** Events log **deliberate
library-level actions** — one command invocation = one entry. Fine-
grained kb-write ops (tag add, thought create, ai_zone append) go
to `audit.log`; events.jsonl is the landmark trail that
`kb-mcp report` aggregates. A fifth report section queries live
Zotero state (not event-based).

Per-paper failures (diagnostic trail):

- `fulltext_skip` — every paper that failed fulltext processing.
  Categories: `quota_exhausted`, `llm_bad_request`, `llm_other`,
  `pdf_missing`, `pdf_unreadable`, `already_processed`,
  `longform_failure`, `other`. Written by kb-importer's short + long
  pipelines.
- `re_read` — every paper picked by `kb-write re-read`, success or
  skip, plus the selector used. Categories: `success`,
  `skip_mtime_conflict`, `skip_llm_error`, `skip_pdf_missing`,
  `skip_not_processed`, `dryrun_selected`.
- `re_summarize` — every single-paper `kb-write re-summarize` run.
  Categories: `success`, `no_change`, `skip_mtime_conflict`,
  `skip_llm_error`, `skip_pdf_missing`, `skip_not_processed`.
  Distinguished from `re_read` so the report can separately count
  "N papers I asked about" vs "M papers the batch selector picked
  for me".

Command-invocation summaries (big-operation landmarks):

- `import_run` — one per `kb-importer import` command. Summary of
  the entire batch (NOT one per paper). Categories: `ok` / `partial`
  / `aborted`. `extra` carries target, metadata pass/fail counts,
  whether fulltext was attempted, and filter context (collection,
  tag, year, all-pending vs all-unprocessed).
- `citations_run` — one per `kb-citations {fetch,link,refresh-counts}`
  invocation. Categories: `ok` / `partial` / `aborted`. The
  `extra.subcommand` field disambiguates which subcommand ran.
- `index_op` — one per `kb-mcp reindex --force` or `kb-mcp snapshot
  {export,import}`. Categories: `ok` / `failed`. **Ordinary
  incremental `kb-mcp index` is NOT logged** — it fires implicitly
  on every MCP tool call and would flood the log with noise.

Live-scan section (not an event):

- **orphans** — md files / attachment dirs with no Zotero
  counterpart, checked NOW via kb-importer + Zotero API. Present
  state, not historical trail. Degrades gracefully if Zotero is
  unreachable.

**Typical use:**

```bash
kb-mcp report                                 # default 30-day window, all 5 sections
kb-mcp report --days 7                         # last week
kb-mcp report --since 2026-04-01
kb-mcp report --sections ops                  # just big-ops summary
kb-mcp report --sections skip                 # one section only
kb-mcp report --sections ops,skip,re_read
kb-mcp report --sections orphans              # just live Zotero scan
kb-mcp report --include-normal                # include already_processed
kb-mcp report --out /path/digest.md
```

The same logic is exposed as MCP tool `kb_report(days, sections,
include_normal)` for agents to pull a digest mid-conversation.

**Output shape** (skip section example):

```
## Fulltext skips  (2026-03-24 → 2026-04-23, 30 day(s))
_(excludes already_processed — pass --include-normal to see)_

Total: 42 skip event(s) across 37 paper(s).

By category:
  - quota_exhausted: 15  (top: ABCD1234, EFGH5678, IJKL9012, +2 more)
  - pdf_missing:     12  (top: MNOP3456, QRST7890)
  - llm_bad_request:  8  ...

Last event: 2026-04-23T10:14:32Z (ABCD1234 → quota_exhausted)
```

Normal skips (`already_processed`) are filtered by default — a
completed paper being "skipped because it's done" isn't a problem.
Pass `--include-normal` to see them.

Adding a new report section later is a one-file change: write a
function `(kb_root, start, end, **opts) → str` and register it in
`SECTION_REGISTRY` in `kb_mcp/tools/report.py`.

## Install

```bash
cd kb_mcp
pip install -e .
```

Requires Python 3.10+.

## Configure

Minimal config — only `kb_root` is mandatory. Via CLI:

```bash
kb-mcp --kb-root /path/to/ee-kb
```

Via env var:

```bash
export KB_ROOT=/path/to/ee-kb
kb-mcp
```

Via config file `<workspace>/.ee-kb-tools/config/kb-mcp.yaml`:

```yaml
kb_root: /path/to/ee-kb
logging:
  level: info
  file: /path/to/ee-kb/.kb/mcp-log.jsonl  # optional
```

## Wire into Claude Desktop

Add to `claude_desktop_config.json` (path varies by OS):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ee-kb": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "kb_mcp", "--kb-root", "/absolute/path/to/ee-kb"]
    }
  }
}
```

The `command` should point to the Python inside the venv where you
installed kb-mcp. Restart Claude Desktop completely (quit from tray,
not just close window) for it to pick up the config.

## Wire into Cursor / Claude Code

Cursor: Settings → MCP → add a server with the same command/args form.

Claude Code: `claude mcp add ee-kb /absolute/path/to/python -- -m kb_mcp --kb-root /absolute/path/to/ee-kb`

## Verify

In a Claude Desktop conversation:

> List the files in my KB under `papers/`.

The AI should call `list_files` with `subdir="papers"`. If it
doesn't show any hits, the wiring is off — check Claude's MCP logs.
