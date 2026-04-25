# ee-kb-tools

Five Python packages that implement the `ee-kb` personal
knowledge-base system. They communicate through the shared markdown
format on disk and (optionally) the SQLite projection inside the KB.

**Current release: 1.4.x.** Latest changes in
[`CHANGELOG.md`](CHANGELOG.md). See the highlights in the per-package
sections below; cross-cutting features (CWD-based workspace autodetect,
OpenRouter for both embedding and fulltext, three modes for
re-summarize / re-read, batch-loop circuit breaker, `kb-importer
preflight`) accumulated through 1.0–1.4.

> Start with [`DEVELOPMENT.md`](DEVELOPMENT.md) — it covers the
> install-and-use flow for everyone (contributors, solo users,
> anyone running the CLI on their own machine).
>
> [`DEPLOYMENT.md`](DEPLOYMENT.md) is a narrower document for
> the specific case of installing the code into someone else's
> workspace (e.g. when an LLM agent sets up a user's machine
> from a handoff). It uses `scripts/deploy.sh` to put the code
> inside a `.ee-kb-tools/` directory next to their KB.
>
> [`UPGRADING.md`](UPGRADING.md) is for moving an existing
> workspace from one version to a newer one — schema bumps,
> config migrations, rollback, the whole procedure.

```
                       ┌─ live mode: localhost:23119 (Zotero running)
Zotero metadata ───────┤
                       └─ web  mode: api.zotero.org (anywhere with net)
                                │
                                ▼
                         kb-importer  ───▶  md files  ◀───  kb-mcp  ───▶  AI clients
                         (you run)          (KB repo)          ▲
                                ▲                              │
                                │                       kb-write (CLI + Python API
                                │                         that AI agents call via
                                │                         MCP tools or shell)
                  PDFs ─── local zotero_storage_dir
                          (rsync'd to server if using web mode)

                                              kb-citations
                                            ┌────────────────────────┐
                                            │ Semantic Scholar /      │
                                            │ OpenAlex  ──▶  cache    │
                                            │         ─▶ links table │
                                            │         ─▶ citation_count│
                                            └────────────────────────┘
```

## Contents (5 packages)

- [`kb_core/`](kb_core/) — shared utilities (workspace autodetect,
  events log, thoughts writer). Other packages import from this; it
  is not a user-facing CLI.

- [`kb_importer/`](kb_importer/) — reads Zotero (web API by default
  or local live API) and writes KB markdown files. Run manually after
  adding papers.

- [`kb_mcp/`](kb_mcp/) — indexer + MCP server that exposes the KB
  to AI clients. FTS5 + vector search + link graph over the md files.
  36 MCP tools covering read + search + write + prefs + graph +
  citations + reporting.

- [`kb_write/`](kb_write/) — client-agnostic write layer. CLI for
  humans, Python API for kb-mcp's write tools. All writes go through
  here: validate → mtime guard → atomic replace → git commit →
  reindex. Also scaffolds the KB (`kb-write init`).

- [`kb_citations/`](kb_citations/) — fetches paper-to-paper citation
  edges from Semantic Scholar or OpenAlex and writes them into
  kb-mcp's link graph + updates per-paper citation counts.

The five packages share the markdown format contract and
(optionally) the SQLite schema. What "independent" means here, more
precisely:

- **Data-format decoupled** — each package reads / writes the same
  on-disk shapes (frontmatter keys, audit log format, events log);
  none of them parse another package's in-memory state.
- **Role-decoupled** — kb-importer runs only when you add papers;
  kb-mcp runs as a long-lived server; kb-write is called by agents;
  kb-citations is invoked manually. They never block each other.
- **NOT install-independent**: `kb_importer` hard-depends on
  `kb_write` (reuses its `atomic.atomic_write` and commit path).
  `kb_citations` has a two-tier dependency on `kb_mcp`:
  `kb-citations fetch` runs standalone (produces a JSONL cache and
  will use that as a fallback if no DB is reachable), but
  `kb-citations link` and `kb-citations refresh-counts` need
  `kb_mcp` installed to write into the projection DB —
  `pyproject.toml` therefore pins `kb-mcp` under the `link` extra
  rather than as a hard dependency. `kb_mcp` soft-depends on
  `kb_write` (write-MCP-tools become unavailable if kb_write isn't
  installed, but the server still starts). `pyproject.toml` in each
  package records these as real dependencies.

Practically: for a full install, install all five into one venv.
For a read-only deployment (e.g. a web server that hosts kb-mcp over
MCP but doesn't write), you can install just `kb_mcp` + your
embedding provider extras and skip the rest.

See [`kb_write/AGENT-WRITE-RULES.md`](kb_write/AGENT-WRITE-RULES.md)
for the normative rules every agent must follow.

### Two LLM configurations, never mixed

The toolchain calls LLMs in two very different places, and the two
configurations are deliberately separate:

| Purpose | Component | Configured in | Provider choices | API key env var |
|---------|-----------|---------------|------------------|-----------------|
| **RAG / vector index** (short-text embeddings for semantic search + graph) | `kb-mcp` | `.ee-kb-tools/config/kb-mcp.yaml` `embeddings:` section | `openai`, `gemini`, `openrouter` | `OPENAI_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_EMBEDDING_API_KEY` (→ falls back to `OPENROUTER_API_KEY`) |
| **Paper fulltext summarization** (7-section JSON summary from PDF) | `kb-importer --fulltext` | `.ee-kb-tools/config/kb-importer.yaml` + CLI flags | `gemini`, `openai`, `deepseek`, `openrouter` | `GEMINI_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY` |

Changing the embedding provider never alters fulltext-summary
behavior, and vice versa — they don't share config keys.

**OpenRouter specifically uses two different env vars on purpose**:
`OPENROUTER_EMBEDDING_API_KEY` for kb-mcp's embedding pipeline,
`OPENROUTER_API_KEY` for kb-importer's fulltext summarizer. Lets
you route the two pipelines at different OpenRouter accounts
(different billing / different rate limits). Single-key users who
only set `OPENROUTER_API_KEY` get both working — the embedding side
falls back transparently when the embedding-specific var is unset.

## Architecture

**Data model.** The KB directory layout:

```
ee-kb/
├── papers/                        # external content — all kind=paper
│   ├── ABCD1234.md                # regular paper
│   ├── BOOKKEY.md                 # whole book / long article
│   ├── BOOKKEY-ch01.md            # chapter (shares zotero_key=BOOKKEY)
│   └── BOOKKEY-ch02.md
├── topics/
│   ├── standalone-note/           # (was zotero-notes/ in v25) rare
│   │   └── <note_key>.md
│   └── agent-created/             # (was topics/<slug>.md in v25)
│       └── <slug>.md
└── thoughts/                      # AI dated thoughts
    └── YYYY-MM-DD-<slug>.md
```

Changes from v25:
  - `zotero-notes/` → `topics/standalone-note/`.
  - Top-level `topics/<slug>.md` → `topics/agent-created/<slug>.md`.
  - Book / long-article chapters moved from
    `thoughts/<date>-<KEY>-chNN-*.md` (kind=thought) to
    **`papers/<KEY>-chNN.md` (kind=paper)**, sharing the parent's
    Zotero key. Each chapter is a first-class paper — searchable,
    linkable, individually re-summarisable.
  - Schema bumped to v6: `papers` PK changed from `zotero_key` to
    `paper_key` (= md stem). `zotero_key` remains as a non-unique
    indexed column so multiple mds can share one Zotero item.
    **`kb-mcp reindex --force` required after upgrade.**
  - **Content at legacy v25 paths is NOT auto-migrated.** The
    indexer skips it; `kb-mcp index-status` reports it under
    "v25 legacy paths (NOT indexed in v26)". Move, rewrite, or
    delete those files per the v26 layout.

**kb-importer**: full import flow, metadata + fulltext.
  - `status`, `list`, `import papers`, `import notes`, `sync`,
    `check-orphans`, `show-template`, `import-summaries`,
    `set-summary`.
  - Two Zotero source modes: `live` (local API, Zotero running) and
    `web` (api.zotero.org). Both produce identical md output.
  - `--fulltext` runs two LLM pipelines on PDFs:
    - **short** (articles): one 7-section summary per paper, written
      into the `## AI Summary (from Full Text)` region of the paper md.
    - **long** (books / theses, item_type-gated): chapter split
      (bookmarks → regex → LLM fallback) → one **paper md per chapter**
      at `papers/<KEY>-chNN.md` (v26; was thought md in v25),
      with a chapter-index table written back to the parent paper md.
  - Default provider **Gemini** (3.1-pro-preview → auto-fallback to
    2.5-pro on daily-quota exhaustion via `--fulltext-fallback-model`).
    OpenAI and DeepSeek also supported.
  - Auto-commits to git (`--no-git-commit` opts out). Three commit
    granularities: metadata-batch per-run, fulltext per-paper,
    longform per-book.
  - **v26 events log**: every paper that fails fulltext
    processing (quota exhausted, LLM 400, PDF missing, PDF
    unreadable, longform chapter failure, unexpected error) is
    appended as a structured JSONL entry to
    `<kb_root>/.kb-mcp/events.jsonl` (event_type=fulltext_skip).
    The same log also records `re_read` events from
    `kb-write re-read`. Auto-included in `kb-mcp snapshot`, not
    git-tracked, readable with `jq` or the
    `kb_importer.events.read_events(kb_root)` helper. Skip
    categories: `quota_exhausted`, `llm_bad_request`, `llm_other`,
    `pdf_missing`, `pdf_unreadable`, `already_processed`,
    `longform_failure`, `other`. stderr prints stay for real-time
    feedback; the JSONL is consumed by `kb-mcp report` for
    periodic aggregation.

**kb-mcp**: **36 MCP tools** (v26: +`list_paper_parts`; v26.x:
+`kb_report`), full SQLite projection + FTS5 + vectors + link
graph + citation integration.
  - `serve` — stdio MCP server for Claude Desktop / Cursor / etc.
  - `index` — build / refresh the projection DB from md.
  - `reindex --force` — drop and rebuild from scratch (required
    for v25 → v26 upgrade, and to change embedding model).
  - `index-status [--deep]` — diagnose staleness and legacy data.
    v26: reports v25 legacy paths ("NOT indexed in v26").
    `--deep` runs SQLite `PRAGMA integrity_check` to catch bit-rot.
  - `list_paper_parts(zotero_key)` — **new in v26**: list all
    mds under papers/ sharing a Zotero key (whole-work + chapter
    siblings). For single-md papers returns one path; for
    split-into-chapters works, returns parent + every `<KEY>-chNN.md`.
    `find_paper_by_key` still returns only the whole-work md.
  - `report` — **new in v26.x**. Periodic operational digest built
    on `<kb_root>/.kb-mcp/events.jsonl` (fulltext skips + re-read
    outcomes). `--days N` / `--since ISO` / `--sections ops,skip,re_read,re_summarize,orphans`
    / `--out file.md` / `--include-normal`. Also available as MCP
    tool `kb_report` for the agent to pull a digest mid-conversation.
  - `snapshot export <tar>` / `snapshot import <tar>` — save and
    restore the whole `.kb-mcp/` directory (projection DB + citation
    cache + similarity prior + **v26 events log**) as a tar. See
    "Backup & restore" below.
  - `similarity-prior-save` / `similarity-prior-compare` — capture a
    model-agnostic top-K neighbour snapshot before changing embedding
    model; verify the new model's neighbours roughly match afterwards.

**kb-write**: client-agnostic write layer. Subcommands covering
scaffold / create / update / delete / prefs / graph / lint / re-read.
  - `init [--refresh]` — scaffold `CLAUDE.md` / `AGENTS.md` /
    `.agent-prefs/` and (if the `.ee-kb-tools/` sibling exists)
    the workspace `config/*.yaml` templates. **v26**: init creates
    `topics/standalone-note/` and `topics/agent-created/` (two-
    level layout).
  - `thought` / `topic` / `pref` — create, update, delete nodes.
  - `ai-zone append` — **v26: replaces v25's full-replace `update`**.
    Inserts a dated `### YYYY-MM-DD — <title>` entry at the TOP of
    the ai-zone (newest first). Older entries preserved verbatim;
    append-only semantics match how Zotero's own notes grow.
    `ai-zone show` prints the current zone + mtime.
  - `re-summarize <paper-key>` — **new in v26**. One paper at a
    time. Runs a fresh LLM pass, judges each of the 7 sections
    new-vs-old, splices only the sections where the LLM judges the
    new text more correct. Preserves the 7-section structure
    bit-for-bit; only section bodies change. Supports book
    chapters (`papers/BOOKKEY-ch03`).
  - `re-read` — **new in v26.x**. Batch re-summarise N papers chosen
    by a pluggable selector (`unread-first` default, plus `random`,
    `stale-first`, `never-summarized`, `oldest-summary-first`,
    `by-tag`, `related-to-recent`). Every outcome writes an event
    to `events.jsonl` for `kb-mcp report` to aggregate. Use
    `--dry-run-select` to preview picks without calling the LLM.
  - `tag`, `ref` — append-only edits inside the safe
    `<!-- ai-zone -->` region and frontmatter surfaces agents own.
  - `delete` — safe delete (rejects protected paths, path traversal,
    escape via `..` / symlinks).
  - `log` — tail of the audit log.
  - `doctor [--fix]` — H checks across the KB; auto-fix scaffold +
    AI-zone markers. v26: aware of the new directory layout.
  - `rules` — print the AGENT-WRITE-RULES contract.
  - `--no-git-commit` flag on all write commands for dry-run-style
    experimentation.

**kb-citations**: 7 subcommands, paper-to-paper graph + counts.
  - `fetch` — pull references (and, optionally, citations) from
    Semantic Scholar or OpenAlex into a local cache.
    `--freshness-days N` skips papers whose cache is newer than N
    days (0 forces refetch); `--with-citations` doubles API cost
    to also fetch incoming citers.
  - `link` — resolve cached DOIs to local paper keys and write
    `origin='citation'` edges into kb-mcp's `links` table. v26:
    `@cite`-style edges resolve to the whole-work paper (never a
    chapter), so a book's citation count doesn't get divided
    between its chapter rows.
  - `refresh-counts` — bulk-update `papers.citation_count` + source
    + timestamp from the provider's paper-meta endpoint. Cheaper
    than `fetch` — one call per paper, no reference-list walk.
    `--max-api-calls N` caps total provider calls. v26: only the
    whole-work row gets updated (chapter rows leave
    `citation_count` NULL).
  - `refs <key>` / `cites <key>` — print cached references /
    citations for one paper.
  - `status` — cache summary (how many papers fetched, how fresh).
  - `suggest --min-cites N` — reading-list emitter: DOIs cited by
    many local papers but not yet in the library. Purely local
    (reads cache, no API).

## Install

The five packages (`kb_core`, `kb_importer`, `kb_mcp`, `kb_write`,
`kb_citations`) install into a **single venv** inside `.ee-kb-tools/`.
This keeps the entire toolchain self-contained in the workspace —
nothing under `~/.venvs/`, `~/.local/`, or any system path.

```bash
# From the workspace parent (the directory that contains
# .ee-kb-tools/, ee-kb/, zotero/ as siblings):
cd .ee-kb-tools

python -m venv .venv
source .venv/bin/activate

# Install all five packages editable. Order matters: kb-core is
# the shared dependency and must come first so the others resolve
# its pinned version from the local checkout. The [write,gemini]
# extras pull kb-write (for write tools via MCP) and google-genai
# (for the Gemini embedding provider). Omit `gemini` if you only
# use OpenAI.
pip install -e ./kb_core
pip install -e ./kb_write
pip install -e "./kb_mcp[write,gemini]"
pip install -e ./kb_importer
pip install -e ./kb_citations
```

Python 3.10+ required.

### Verify the install

```bash
python scripts/post_install_test.py
```

Runs 16 smoke tests covering all four user-facing CLIs, workspace
initialization, write operations, indexing, API connectivity, and
the no-system-path lint. Exit 0 = healthy. API tests for OpenAI /
Gemini / Semantic Scholar are skipped if their keys aren't set —
they never block the exit.

### Why one venv, not five

The five packages aren't independent: `kb-mcp` soft-imports
`kb_write` to expose write tools over MCP, and soft-imports
`kb_citations` to expose citation trigger tools. Without these on
the path, those tools silently disappear from the MCP surface.
Separate venvs would break those links. One venv in
`.ee-kb-tools/.venv/` keeps the five talking and keeps the
workspace portable — move the parent directory and everything
still resolves via the sibling autodetect.

### Activating

Any shell that uses the tools needs this venv active:

```bash
source <workspace>/.ee-kb-tools/.venv/bin/activate
```

Or add a shell alias / direnv `.envrc` so the venv auto-activates
when you `cd` into the workspace.

### API keys

API keys go in your shell rc (`~/.bashrc` / `~/.zshrc`), never in
config files or the venv. The YAML configs under
`.ee-kb-tools/config/` reference keys by env var name only.

```bash
# in ~/.bashrc
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...          # optional, only if provider=gemini
export ZOTERO_API_KEY=...          # optional, only for kb-importer web mode
```

## Bootstrapping a new KB

Step 1 — Set up the canonical workspace layout.

```bash
mkdir -p workspace/{.ee-kb-tools,ee-kb,zotero/storage}
# (parent directory may be named anything — `workspace` is just an
# example; `research`, `docs`, wherever you keep work.)

cd workspace/.ee-kb-tools
git clone <repo-url> .        # or copy the five package directories in
python -m venv .venv
source .venv/bin/activate
# kb_core pins the cross-package version; install it first so the
# others resolve their `kb-core==` dep from the local checkout.
pip install -e ./kb_core
pip install -e ./kb_write
pip install -e "./kb_mcp[write,gemini]"
pip install -e ./kb_importer
pip install -e ./kb_citations
```

Step 2 — Initialize `ee-kb/` scaffolds.

```bash
cd ../ee-kb
git init
kb-write init             # creates CLAUDE.md / AGENTS.md / README.md /
                          # .cursorrules / .aiderrc / AGENT-WRITE-RULES.md /
                          # .agent-prefs/README.md
                          # and scaffolds ../.ee-kb-tools/config/*.yaml
```

Step 3 — Pick a kb-importer mode and run it.

### Mode A: live (Zotero running on same machine)

```bash
# Make sure Zotero is running with local API enabled.
# Zotero settings → Advanced → "Allow other applications..."

kb-importer status        # autodetects kb_root + zotero_storage from
                          # sibling relationship; no flags needed
kb-importer list papers --limit 5
kb-importer import papers --collection "Core"
```

### Mode B: web (Zotero API, e.g. headless server)

```bash
# One-time: copy Zotero's storage directory to the workspace.
rsync -av ~/Zotero/storage/ /path/to/workspace/zotero/storage/

# Export the API key (from https://www.zotero.org/settings/keys) via
# shell rc — ZOTERO_API_KEY is the default var name kb-importer reads.
export ZOTERO_API_KEY=<your-read-only-key>
export KB_ZOTERO_SOURCE=web
export ZOTERO_LIBRARY_ID=<your-userID>

kb-importer status
```

Step 4 — Index and wire up MCP.

```bash
kb-mcp index                      # builds SQLite + vectors + link graph
kb-citations fetch                # optional: pull paper-to-paper
                                  # citation edges from Semantic Scholar
kb-citations link                 # apply edges to kb-mcp links table
```

Then point Claude Desktop / Cursor / whatever MCP client at
`kb-mcp serve`. See [`kb_mcp/README.md`](kb_mcp/README.md) for the
exact MCP config snippet.

## Backup & restore

The projection DB (`ee-kb/.kb-mcp/index.sqlite`) and its sidecar
caches (`ee-kb/.kb-mcp/citations/`, `ee-kb/.kb-mcp/similarity-prior.json`)
are **derived data** — they can always be rebuilt from the md files
via `kb-mcp index --force` and re-running `kb-citations fetch`. So
backups are optional in a strict correctness sense.

In practice, though, an embedding rebuild costs real API money and
several minutes per thousand papers, and citation-fetch costs a
separate API round-trip per paper with provider rate limits. Backing
up `.kb-mcp/` lets you skip that cost on any recovery.

**Export a snapshot** (whole `.kb-mcp/` directory into one tar):

```bash
kb-mcp snapshot export ~/backup/kb-snapshot-$(date +%Y-%m-%d).tar
# .tar.gz suffix → gzip compression:
kb-mcp snapshot export ~/backup/kb-snapshot-$(date +%Y-%m-%d).tar.gz
```

The export uses SQLite's `VACUUM INTO` so the DB inside the tar is
always consistent, even if `kb-mcp serve` is running concurrently.

**Restore a snapshot** (puts the DB and caches back):

```bash
kb-mcp snapshot import ~/backup/kb-snapshot-2026-04-23.tar
# Refuses to overwrite a non-empty .kb-mcp/ without --force.
```

**Scope of what's in the tar** (contract — stable across versions):

- `.kb-mcp/index.sqlite` — projection DB (papers, links, chunks, vectors)
- `.kb-mcp/citations/by-paper/*.json` — provider fetch cache
- `.kb-mcp/similarity-prior.json` — model-agnostic neighbour prior (if present)

**What's NOT in the tar** (covered by your existing tools):

- `ee-kb/papers/*.md` and the rest of `ee-kb/` — git repo.
- `zotero/storage/**` — rsync / Zotero sync.
- `.ee-kb-tools/config/*.yaml` — per-machine; you set these up during
  install.

A typical cron rhythm for the `ee-kb/` + `.kb-mcp/` pair:

```
# ee-kb (source of truth) — already in git; push to remote
*/30 * * * * cd /path/to/ee-kb && git add -A && git commit -m auto-$(date +\%s) && git push origin main

# .kb-mcp/ (derived but expensive to rebuild) — daily tar, keep 7 days
0 3 * * * kb-mcp snapshot export /path/to/backup/kb-$(date +\%Y-\%m-\%d).tar.gz && find /path/to/backup -name 'kb-*.tar.gz' -mtime +7 -delete
```

## Maintenance commands

Beyond day-to-day import / index, the toolchain has several
diagnostic and recovery commands:

**`kb-mcp index-status`** — per-table row counts, how many papers
have fulltext summaries, how many mds have drifted from the DB
(staleness). Run this first when "why can't I find X?" strikes.

**`kb-mcp index-status --deep`** — additionally runs SQLite's
`PRAGMA integrity_check` across every page of the DB file. Catches
filesystem-level corruption (bit-rot, torn writes) that ordinary
queries don't surface. Slow; run on demand, not routinely.

**`kb-mcp reindex --force`** — delete `.kb-mcp/index.sqlite` and
rebuild from md. Needed when switching embedding model or provider;
safe any time (DB is derived from md). Combine with
`--provider openai --model text-embedding-3-large --dim 3072` to
pick a new embedding at the same time.

**`kb-mcp similarity-prior-save` / `...-compare`** — when you're
about to change embedding model and want to know whether the new
model's "what's similar to X?" answers roughly agree with the old
model's. Capture the prior BEFORE switching; compare AFTER. A low
Jaccard score means the switch meaningfully reordered your graph.

**`kb-write doctor [--fix]`** — lint pass over the whole KB:
scaffold presence, AI-zone markers, slug format, dangling kb_refs,
frontmatter field types. `--fix` auto-repairs scaffold and empty
AI-zone markers. Other findings are reported but not auto-fixed
(they need a human decision).

**`kb-write log [N]`** — tail the audit log (last N operations).
Useful after an agent-driven session to review what got touched.

**`kb-write migrate-legacy-chapters [--dry-run]`** — one-shot
migration of pre-v24 longform chapter thoughts from
`thoughts/<date>-<KEY>-ch<NN>-<slug>.md` into the v26 canonical
location `papers/<KEY>-chNN.md`. No LLM call; body is preserved
verbatim. Idempotent — re-runs detect already-migrated chapters
and skip. Collisions (target exists with a different chapter)
are reported, not overwritten. All moves land in a single
batch git commit. Use `--dry-run` to preview the plan without
touching any files.

**`kb-citations status`** — cache summary (how many papers fetched,
cache age). Quick answer to "does kb-citations have anything for
paper X?".

**Events log (v26)** — `<kb_root>/.kb-mcp/events.jsonl` is an
append-only JSONL of structured events. **Events are deliberate,
library-level actions** — one command invocation = one entry, not
one per paper. Fine-grained kb-write operations (tag add, thought
create, ai_zone append) stay in `audit.log`; events.jsonl is the
"I did something big with my library" trail that `kb-mcp report`
aggregates. Six event types:

- `fulltext_skip` — per-paper fulltext failure (quota, LLM 400,
  PDF missing/unreadable, longform chapter failure). Written by
  kb-importer's short + long pipelines. Also useful as a
  diagnostic trail even though technically per-paper.
- `re_read` — per-paper outcome inside a `kb-write re-read` batch.
- `re_summarize` — single-paper `kb-write re-summarize` outcome
  (success / no-change / various skips).
- `import_run` — one per `kb-importer import` command invocation.
  Summary of the whole batch: metadata-only / +fulltext, with
  filter context (--all-pending / --collection / --tag / --year /
  explicit keys / --all-unprocessed). Category = `ok` / `partial`
  / `aborted`.
- `citations_run` — one per `kb-citations {fetch,link,refresh-counts}`
  invocation. `extra.subcommand` disambiguates which.
- `index_op` — one per `kb-mcp reindex --force` or `kb-mcp
  snapshot {export,import}`. Ordinary (incremental) `kb-mcp index`
  is NOT logged — it runs implicitly on every MCP tool call and
  would flood the log.

Not git-tracked. Auto-included in `kb-mcp snapshot`. The aggregator
below is the intended user-facing reader; raw JSONL is also fine
for ad-hoc `jq` queries.

**`kb-mcp report`** — periodic operational digest. Five sections
today (plug-in registry, easy to extend):

- **`ops`** — "library-level operations" in window: how many times
  you ran `kb-importer import`, `kb-citations {fetch,link,refresh}`,
  or `kb-mcp {reindex,snapshot}`. Merges `import_run` + `citations_run`
  + `index_op` events into one at-a-glance block. This is the
  "what big things did I do this month" answer.
- **`skip`** — fulltext processing skips from `events.jsonl`
  (kb-importer failures by category: quota, PDF, 400, etc.).
  Normal skips (`already_processed`) excluded by default — pass
  `--include-normal` to see them.
- **`re_read`** — batch `kb-write re-read` outcomes over the window
  (success / skip counts; selector usage).
- **`re_summarize`** — single-paper `kb-write re-summarize` outcomes.
  Distinguishes `success` (sections spliced) from `no_change` (LLM
  agreed with stored text).
- **`orphans`** — LIVE scan (reaches out to Zotero): md files /
  attachment dirs with no Zotero counterpart. Degrades gracefully
  if Zotero is unreachable. Unlike the others this isn't historical
  — "is X orphan" is always a "right now" question.

```
kb-mcp report                                 # default: all 5 sections, 30-day window
kb-mcp report --days 7                        # last week
kb-mcp report --since 2026-04-01
kb-mcp report --out digest.md                 # write to file
kb-mcp report --sections ops                  # just big-ops summary
kb-mcp report --sections ops,skip             # pick sections
kb-mcp report --sections orphans              # just live orphan scan
kb-mcp report --include-normal
```

Also available as MCP tool `kb_report` — the agent can pull a
report during a conversation.

**`kb-write re-read`** — batch re-summarize N papers chosen by a
pluggable selector. Reuses the v26 single-paper
`kb-write re-summarize` internally; re-read is the auto-selection
+ batch dispatch layer on top. Every outcome writes an event to
`events.jsonl` so `kb-mcp report` can aggregate trends.

Selectors (use `--list-selectors` for full help):

| selector | picks |
|---|---|
| `unread-first` (default) | Papers never re-read before; falls back to random |
| `random` | Uniform random sample |
| `stale-first` | Oldest md_mtime first |
| `never-summarized` | Only fulltext_processed != true |
| `oldest-summary-first` | Oldest `fulltext_extracted_at` from frontmatter |
| `by-tag` | Requires `--selector-arg tag=<name>` |
| `related-to-recent` | Graph neighbours (via kb_refs / citation) of papers you recently touched |

Candidate sources:

- `--source papers` (default): all paper mds
- `--source storage`: only papers whose PDF is on disk in
  `zotero_storage/` AND have an imported md

Typical usage:

```
kb-write re-read                                 # default: 5 papers, unread-first
kb-write re-read --count 10 --selector stale-first
kb-write re-read --selector by-tag --selector-arg tag=foundational
kb-write re-read --selector related-to-recent --selector-arg anchor_days=7
kb-write re-read --dry-run-select                # preview without LLM
kb-write re-read --seed 42                       # reproducible
```

