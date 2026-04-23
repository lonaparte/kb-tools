# Changelog

All notable changes to ee-kb-tools.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is our own (calendar-ish, per-major-iteration).

## [0.27.1] — 2026-04

Second bug-fix pass on the v0.27 line, responding to a second
round of field testing on a 1154-paper library. Adds three test-
coverage improvements (static schema FK lint, broadened
`test_report_generation` for real libraries, re-summarize
classifier regression suite) plus one event-classification fix.

**Versioning note.** This release introduces proper 0.x.y
semver. Previous releases shipped with mixed version strings
(`VERSION=27`, package `__version__="27"`, pyproject
`version="0.1.0"`). All five packages now ship coordinated at
`0.27.1`. Releases will be `0.x.y` until the KB spec and MCP
tool surface stabilise enough to warrant a `1.0.0`.

### Fixed

- **`re-summarize` failure classifier sent too many events to
  `skip_llm_error`.** Two v26.5 field-report cases:
  (a) `"paper md not found: papers/NOT_A_REAL_KEY.md"` — user
      typo / bad argument, LLM never called, but classified as
      an LLM failure; (b) `"no zotero_attachment_keys in
      frontmatter — cannot locate the PDF"` — PDF-locate
      failure, but the classifier's substring match required
      "missing" / "not found" / "no pdf" and missed the phrase
      "cannot locate".
  Fixes: (1) a new `skip_bad_target` category for
  user-error / missing-md cases (LLM never reached);
  (2) broader substring patterns for PDF-locate failures;
  (3) `.code=` attributes on the raise sites so classification
  doesn't depend on substring matching at all for well-known
  modes. Locked by
  `tests/unit/test_re_summarize_classifier.py`.

### Test infrastructure

- **`scripts/test_e2e.py` gains a static schema-FK lint.**
  `test_schema_fk_targets_are_pk_or_unique` parses
  `schema.sql`, collects PK / UNIQUE columns on `papers`, and
  asserts every `REFERENCES papers(<col>)` targets one of
  them. This lint would have caught the v26 FK bug (four
  side-tables pointing at `papers(zotero_key)` after the v6 PK
  change); it was missing because the existing
  `test_sql_joins_use_paper_key` test only scanned `.py` files.
- **`test_schema_accepts_upsert_with_fk_on` added.** Reproduces
  the v26 FK failure pathway end-to-end: `PRAGMA
  foreign_keys=ON` + `INSERT ... ON CONFLICT(paper_key) DO
  UPDATE SET zotero_key = ...` (the indexer's real call
  shape). The pre-existing `test_book_chapter_schema` only
  used raw INSERT with FK checks disabled; the v26 bug was
  invisible to it.
- **`test_report_generation` orphans assertion broadened.** The
  section has four legitimate output shapes (including "found
  N orphans" — which the field-test library triggered with
  1218 archived attachments); v26 only covered three, so
  realistic libraries failed the test. Added the fourth.

### Known residual items (v0.28 scope)

The v26.5 field report flagged several items that are logged
here but deferred:

- **`index-status --deep` flags 182 legacy chapter-in-thoughts
  mds but offers no `--fix`.** These are pre-v24 long-article
  chapters that belong in `papers/<KEY>-chNN.md` (v26 layout)
  but still sit in `thoughts/<date>-<KEY>-ch<NN>-*.md`. A
  `kb-write migrate-legacy-chapters` tool is the right
  solution but hasn't been written — for now the check is
  informational only.
- **`kb-write doctor` reports 182 `[slug]` warnings for the
  same legacy chapters**; same fix as above (no `--fix` yet).
- **`kb-importer check-orphans` makes 2 full Zotero API round
  trips (~45s on 1154 items) every call.** No cache / no
  incremental mode — each invocation is a fresh fetch. A
  `--since <ts>` flag or a 5-minute in-memory cache would
  mostly eliminate the wait.
- **`kb-importer --dry-run sync` position.** argparse accepts
  `--dry-run` only BEFORE the subcommand, whereas `kb-mcp`
  accepts it after. Cross-CLI consistency fix, low priority.

## [0.27.0] — 2026-04

Security / release-hygiene follow-up to v26 PLUS critical bug-fix
pass from a v26 production deployment field report. Addresses:

- v26 field-test bugs blocking 100% of library indexing / re-
  summarize / re-read-from-storage (four bugs, three of them hit
  1154/1154 papers in the reporter's library)
- a third-party code review's findings around host-metadata
  leakage, CLI path semantics, and HTTP rate-limit handling
- two items from the v25 review that were still live in v26

Schema version bumps v6 → v7 (foreign-key fix — existing DBs
drop-and-rebuild automatically on first v27 startup). MCP tool
count, selectors, events.jsonl structure unchanged.

### Critical v26 field-report fixes

These four bugs were reported after v26 hit a real 1154-paper
library. Each made a top-level workflow unusable until the user
hand-patched the code. Listed by severity.

1. **Schema v6 foreign-key mismatch (BLOCKING).** `papers.paper_key`
   is the PK since v6's book-chapter rework; `zotero_key` is an
   indexed-but-non-unique column. But four side-table FKs
   (`paper_attachments`, `paper_tags`, `paper_collections`,
   `paper_chunk_meta`) were still written as
   `REFERENCES papers(zotero_key)` — which SQLite refuses with
   "foreign key mismatch" when `PRAGMA foreign_keys=ON`, because
   FK targets must be PK or UNIQUE. The entire indexer would
   crash on every INSERT. v27 repoints all four FKs to
   `papers(paper_key)` and bumps the schema version 6 → 7 so
   existing v6 DBs with the broken FK state get automatically
   rebuilt on first v7 startup (no manual `rm index.sqlite`
   required). Locked by three regression tests in
   `tests/unit/test_schema_fk.py` that apply the real schema,
   enable `PRAGMA foreign_keys=ON`, and exercise all four side
   tables + CASCADE semantics.

2. **`re-summarize` found 0/N attachments on any real library.**
   `_extract_frontmatter_list` parsed block-form lists with
   hardcoded 2-space indent (`  - KEY`), but PyYAML's default
   dump — which `python-frontmatter` uses — emits 0-indent
   (`- KEY`). Every kb-importer-written md therefore looked like
   it had empty `zotero_attachment_keys`, and `re-summarize`
   refused every paper with "no zotero_attachment_keys in
   frontmatter — cannot locate the PDF". Same bug affected
   `authors` parsing, which silently omitted author context
   from the re-summarize LLM prompt.

3. **`re-read --source storage` returned 0 on any real library.**
   Mirror image of bug 2: `_FM_ATTACHMENT_FLOW_RE` in
   `re_read_sources.py` matched ONLY flow-form
   (`key: [a, b, c]`) — but real mds are all block-form. Zero
   hits on any paper.

   Fixes 2 and 3 are consolidated into a single new parser
   `kb_core.frontmatter.extract_list` that handles flow AND
   block (0-indent AND 2-indent), stops at the next top-level
   key, strips one layer of quotes. Both `kb_importer` and
   `kb_write` delegate to it. Locked by 14 cases in
   `tests/unit/test_frontmatter.py`, including the exact real-
   world shape observed in the field report.

4. **ZIP filename vs VERSION inconsistency.** v26 shipped as
   `12-codes-ee-kb-tools-v26.zip`, but the reporter received
   what they believed was a re-uploaded v26 but was actually a
   newer build — they renamed to `v26.5` manually to track it.
   From v27 onward, release ZIPs carry version-incrementing
   filenames (`13-...-v27.zip`, `14-...-v27.1.zip`, etc.) and
   never repeat.

### CLI breaking changes (called out in review)

- **`kb-write thought create/update` / `kb-write topic
  create/update` / `kb-write paper body append/replace` no
  longer accept `--body <str>`.** All take `--body-file
  <path|->` instead. Passing `-` reads from stdin.

  The field report (v26.5) flagged this as a silent breaking
  change from v24. Rationale for the change (not previously
  documented): shell-quoting of multi-line / Unicode content
  is fragile, and agents that shell out to `kb-write` end up
  producing malformed commands with embedded newlines /
  quotes. The stdin / file-only path is unambiguous and safe
  for both interactive users and agent orchestrators.

  **Migration:** any v24-or-earlier script using `--body "..."`
  needs to pipe through stdin:
  ```bash
  # Before (v24)
  kb-write thought create --title X --body "my idea"

  # After (v25+)
  echo "my idea" | kb-write thought create --title X --body-file -
  ```
  Sorry for surfacing this change late — it should have been
  in v25's release notes when it landed.

### UX improvements (from v26.5 field report)

- **`kb-mcp report` / MCP `kb_report` default sections no longer
  include `orphans`.** Field-report observation: the orphan
  detector does a live Zotero API scan (1200+ round-trips for a
  real library), which surprised users who expected an offline
  "quick status" command. New default set is
  `ops, skip, re_read, re_summarize`. Request orphans
  explicitly with `--sections ops,skip,orphans` or
  `--sections all` when you want it.

### Security / information-disclosure

- **`kb-mcp snapshot export` no longer records `kb_root_at_export`
  in the snapshot manifest.** Earlier versions wrote the exporter's
  absolute KB path into `snapshot-manifest.json`, which leaked the
  source machine's home layout / username / deployment path to
  anyone who received the snapshot tar. Manifest version bumped
  1 → 2; readers accept both versions.
- **`kb-write` audit.log no longer records PID or Unix username by
  default.** Both are opt-in via
  `KB_WRITE_AUDIT_INCLUDE_PID=1` / `KB_WRITE_AUDIT_INCLUDE_USER=1`.
  Rationale: audit.log lives inside `.kb-mcp/` and ships with
  `kb-mcp snapshot export`; earlier versions would have leaked
  host identity through shared snapshots.
- **`kb-write` CLI defaults to kb-relative paths in human output**;
  pass `--absolute` for full paths (handy when piping into
  `vim` / `cd`). JSON output was already kb-relative in MCP tools;
  this brings the shell surface in line.

### Robustness

- **Semantic Scholar / OpenAlex HTTP 429 handling honours
  `Retry-After`** (capped at 120s) instead of fixed exponential
  backoff. A badly-phrased 429 with a 10-minute hint no longer
  stalls a 100-paper batch.

### Structural / maintainability

The v27 review flagged five maintainability concerns around
orchestration-layer complexity, duplication, error classification,
and output drift. All five are addressed in this release:

1. **`kb_core` contract layer extracted.** Path safety (`PathError`,
   `safe_resolve`, `to_relative`), KB directory layout constants
   (`PAPERS_DIR` etc.), node addressing (`NodeAddress`,
   `parse_target`, `from_md_path`), schema version constants
   (`SCHEMA_VERSION = 7`, `FULLTEXT_START/END`, `SECTION_COUNT`),
   and workspace resolution (`Workspace`, `resolve_workspace`,
   `find_workspace_root`) now live in a new bottom-of-stack
   package with zero runtime deps. `kb_write.paths` /
   `kb_write.workspace` / `kb_mcp.paths` / `kb_mcp.workspace` are
   thin re-export shims. The old "mirror-and-lint" pattern is
   replaced by a single implementation with `check_package_
   consistency.py`'s new identity check guarding against
   regression.
2. **Structured error codes.** `kb_importer.summarize` now defines
   `BadRequestError`, `PdfMissingError`, `QuotaExhaustedError`
   subclasses of `SummarizerError`, each with a stable `.code`
   attribute. `kb_write.ops.re_summarize.ReSummarizeError`
   accepts `code=` in its constructor. The re-read / re-summarize
   failure classifiers prefer `exception.code` for routing into
   the right event category, with substring-matching as a
   fallback for pre-v27 call sites. A provider changing its
   400-response wording no longer silently re-routes events to
   `llm_other`.
3. **Output formatter module.** `kb_core.format` provides
   `render_path`, `render_error`, `render_json` with a stable
   field order for write-result JSON. `kb_write` CLI routes
   through `render_path`; MCP tools will follow in v27.x /
   v28. The helper is deliberately primitive — no heavy
   framework, no dataclass coupling.
4. **pytest structured test suite.** New `tests/unit/` directory
   with dedicated tests for path safety, addressing, schema,
   shim identity, atomic writes, audit log, events, error
   codes, snapshot round-trip, selectors, format helpers, HTTP
   client reuse, and import lock. 174 tests total. Runs via
   `scripts/run_unit_tests.py` (stdlib-only vendor of a pytest
   subset) so CI works without `pip install pytest`.
   `scripts/test_e2e.py` retained as the integration smoke
   test.
5. **Large-file split: deferred.** `server.py` (~1900 lines),
   `import_cmd.py` (~1500), `indexer.py` (~1200), and
   `kb_write/cli.py` (~1000) still live. The kb_core extraction
   and test-suite scaffolding above are prerequisites for this
   work; attempting the file-split in the same release would have
   produced a much riskier diff. Scheduled for v28 as a pure
   structural pass with no behaviour change.

### Concurrency

- **`kb-importer import` now takes a cross-process lock** at
  `<kb_root>/.kb-mcp/import.lock` (fcntl flock). Two concurrent
  imports on the same KB now cleanly refuse the second with a
  diagnostic pointing at the pid/start-time of the holder,
  instead of racing through md writes. The lock auto-releases on
  process exit (crash-safe). Dry-run skips the lock.

### CLI argument validation

- **Positive-integer validation.** Key count / day / top-k / dim /
  max-refs / max-cites / max-api-calls / min-cites / limit
  arguments across `kb-write` / `kb-mcp` / `kb-citations` now
  reject zero and negatives at parse time instead of silently
  defaulting to empty windows or zero-size batches.

### Graceful shutdown

- **`kb-mcp serve` handles SIGTERM.** Flushes the Store and closes
  the SQLite connection before exiting, preventing WAL/SHM
  residue from container orchestrator kills. SIGINT (Ctrl-C) was
  already handled by `mcp.run()`; this adds the headless-server
  path.

### Test infrastructure

- **`scripts/run_unit_tests.py`**: pytest-compatible runner that
  needs only stdlib. Supports `@pytest.fixture`, `pytest.raises`,
  `pytest.skip`, `pytest.fail`, `@pytest.mark.parametrize`, and
  the built-in `tmp_path` / `monkeypatch` fixtures. Tests also
  run unchanged under real pytest when available.

### Documentation

- **README "four independent packages" clarified.** Data-format
  decoupled and role-decoupled, yes; install-independent, no.
  The actual cross-package dependencies (hard and soft) are
  spelled out. kb_core added as a fifth (internal) package.
- **`kb_importer/README.md` adds "Concurrent runs" section** —
  cron + interactive kb-write may collide with
  `WriteConflictError`; the new `import.lock` now also protects
  against two concurrent `kb-importer import` runs.

### Release artefacts

- `LICENSE` (MIT).
- `CONTRIBUTING.md` covering install, layout, required pre-PR
  checks, style rules (English code/comments, CJK OK in LLM
  prompts), and step-by-steps for adding MCP tools / selectors.
- `pyproject.toml` across all packages gains PyPI classifiers
  and keywords; new `kb_core/pyproject.toml` published at
  version 0.1.0 and referenced as a dependency by the four
  bundle packages.

### v25 review follow-up

The v25 post-release review flagged three issues that were still
live in v26. All three are addressed here:

- **Self-heal boundary (v25 item 1).** The heading-based self-heal
  for pre-v21 mds used any bare `---` line as the end-of-summary
  sentinel, which over-matched: markdown horizontal rules inside
  the summary (section breaks, table separators) would trigger
  early cut-off and silently lose trailing content. v27 tightens
  the sentinel to a `---` line IMMEDIATELY followed by
  `<!-- kb-ai-zone-start -->` (or EOF). Locked by 8 regression
  cases in `tests/unit/test_legacy_fulltext_extraction.py`
  including the exact "internal horizontal rule" scenario.
- **`kb-citations refresh-counts` raw traceback (v25 item 2).**
  `refresh-counts` on a KB without a projection DB used to dump
  a raw `FileNotFoundError` traceback, while `link` had a clean
  message + pointer-to-fix. v27 aligns them: `refresh-counts`
  now emits `kb-citations refresh-counts: cannot update ... /
  Run \`kb-mcp index\` first` to stderr, exits 2. Same for the
  `kb-mcp not installed` soft-import path. Locked by
  `tests/unit/test_refresh_counts_no_db.py`.
- **`kind: zotero_standalone_note` naming (v25 item 7).** The long
  form was inconsistent with `NodeAddress.node_type == "note"`.
  v27 switches `md_builder` to write `kind: note` for all new
  standalone-note imports. `indexer` and `list_files` accept BOTH
  values so the thousand-plus existing pre-v27 mds in real KBs
  don't appear to have vanished. No migration is performed — the
  old value continues to resolve; future re-imports will
  naturally overwrite to the new form. Locked by
  `tests/unit/test_note_kind_compat.py`.

### v25 review — remaining items, explicitly not addressed

These are on the record for v28:

- **30+ concurrent tag writes still lose updates (v25 item 3).**
  Architecturally deeper than "add a retry" — the write_lock
  currently serialises at the file level but tag updates do a
  read-modify-write on the `zotero_tags` list. At high
  concurrency the window between read and mtime-check-then-write
  still admits interleaving. Proper fix is either (a) a
  per-paper lock held across read and write, or (b) rewriting
  tag ops as append-only journal that dedupes on read. Both are
  scope for a v28 design pass.
- **MCP stdio long-session memory (v25 item 4).** Still not
  profiled. v26.5 field report confirms ~+24 MB RSS per 90
  tool calls, essentially unchanged from v22 measurements —
  the shape suggests bounded-per-call allocation that isn't
  being released, not an unbounded cache. Candidates are
  SQLite statement cache growth, LLM/embedding response
  objects retained in module-level state, or the MCP server's
  request-history buffer. Needs a long-running session under
  `tracemalloc` to locate; can't be fixed by inspection alone.
  Hard-scheduled for v28.
- **MCP parameter name alignment (`target` vs `md_path`) (v25
  item 6).** A breaking-ish API change — the current surface
  has both names across different tools. Cleanup belongs with
  the v28 structural pass.
- **Pre-v24 uppercase-slug migration tool (v25 item 5).** Not a
  code bug; data-state issue for users who imported under v22/v23.
  No plan to ship a migration command in the bundle — `kb-write
  migrate-slugs` would be a one-off utility more appropriate as
  a separate script.
- **`--with-citations` edge-count reduction (v25 new finding).**
  Couldn't reproduce or disprove without access to a real
  OpenAlex response stream. May be upstream data variance or a
  linker dedup change; flagged for investigation on next real-
  data run. If confirmed a regression, tracked separately.

### Known technical debt (v28 candidates — not blocking release)

The following items were flagged by the v27 review + a subsequent
self-audit and deferred to a future structural pass. They don't
affect correctness but will bite maintainability if left longer.
Listed by priority.

**P0 (must address in v28):**

1. **File size** — `server.py` (~1900 lines), `import_cmd.py`
   (~1500), `indexer.py` (~1200), `kb_write/cli.py` (~1000). Each
   wants to split into ~3-5 topical submodules. The orchestration
   layer is beginning to "absorb everything" (parsing + business
   logic + error classification + output rendering). Pure
   structural move, no behaviour change; blocks further feature
   growth.
2. **Testing structure** — monolithic `scripts/test_e2e.py` works
   but can't isolate a single case. Migrate to pytest with
   `tests/{unit,integration,e2e}/`. Add dedicated unit tests for
   path safety, atomic write, frontmatter merge, each selector,
   events round-trip, snapshot.
3. **Error classification via string matching** — `re_read` /
   `re_summarize` classify failures by substring-searching the
   exception message ("pdf missing", "mtime conflict", ...). This
   breaks when providers change their wording. Replace with
   structured exception subclasses carrying a `category` attribute.

**P1 (worth including in v28):**

4. **Duplicated path / workspace logic** between kb_write and
   kb_mcp, currently kept in sync by
   `scripts/check_package_consistency.py`. A thin `kb_core`
   contract layer (path layout, `safe_resolve`, workspace
   resolution, schema constants, node types) would eliminate the
   duplication without blurring the four-package boundary. Strict
   rule: `kb_core` holds zero business logic, only constants +
   pure protocol functions.
5. **Output formatter** — CLI / MCP / `kb-mcp report` each
   hand-format strings. Introduce a small
   `render_result / render_paper / render_event` layer
   (no heavy framework). Keeps field ordering, error prefixes,
   date format in sync across all three surfaces.
6. **CLI int-argument validation** — v27 added positive-int
   checks to `--count`, `--days`, `anchor_days`, but other int
   parameters (`--max-refs`, `--max-api-calls`, `--top-k`,
   `--at-k`, `--dim`, `-n`) accept negatives silently. Add an
   argparse `positive_int` helper and apply everywhere.

**P2 (optional, quick wins):**

7. **kb-importer concurrency lock** — `kb-importer import`
   currently has no cross-process lock. A run-level
   `.kb-mcp/import.lock` file would refuse a second concurrent
   import cleanly instead of letting both runs race through
   Zotero → md writes.
8. **`kb-mcp serve` graceful shutdown** — handle SIGTERM: flush
   the store, close the SQLite connection, exit cleanly. Prevents
   wal/shm residue on the server-deployed side.
9. **HTTP client reuse** in kb-citations — each subcommand rebuilds
   `httpx.Client`. For 100+ paper batches a long-lived client with
   keepalive would save latency.
10. **events.jsonl rotation** — the `unread-first` selector reads
    the entire log each run. At ~1k papers × years of use, still
    sub-MB; at 10× scale, consider monthly-rotated files or a
    sqlite index. Record only; defer the work.

**P3 (don't do unless a real use case appears):**

11. **Compile-time `operator surface` vs `agent surface`
    separation** — currently enforced by convention (MCP tools
    return relative paths; CLI `--absolute` opt-in). Stricter
    would need wrapper types with a cost exceeding the safety
    gain for a single-user tool.
12. **Selector plugin loading** — 7 selectors don't justify
    auto-discovery; explicit registry is more debuggable.
13. **Pydantic MCP schemas** — current type-hint → schema auto-
    generation is good enough for a single-user tool. Pydantic
    would add a runtime dep to save ~5 agent-error messages/year.

**P4 (miscellaneous, found during v27 audit):**

14. **`write_lock` timeout hardcoded at 10s** — add
    `--wait-lock N` CLI flag; default 10s stays.
15. **`similarity-prior` versioning** — current
    `similarity-prior.json` overwrites on each save, losing
    history when switching embedding models more than once.
    Version by model-name suffix would let `compare` reference
    older priors.
16. **`list_files(kind_filter=...)` reads frontmatter per file**
    — slow at scale. Add `kind` column to the `papers/notes/
    topics` projection tables (or just to a new view) so SQL
    can filter directly.

## [v26] — 2026-04

First public-facing release. Four packages, coordinated schema v6,
36 MCP tools, 7 re-read selectors, events.jsonl operational log.

### Data model

- **Schema v6**: `papers.paper_key` is the primary key;
  `zotero_key` is non-unique indexed. This lets book chapters
  share a parent `zotero_key` while keeping a unique per-chapter
  `paper_key`.
- **Book chapters as first-class papers**: `papers/<KEY>-chNN.md`
  with `kind: paper`, `item_type: book_chapter`, shared
  `zotero_key` with the parent whole-work md. Chapters are
  searchable, linkable, re-summarizable — just like regular
  papers.
- **New MCP tool `list_paper_parts(zotero_key)`**: list all mds
  under `papers/` sharing a Zotero key (whole-work + chapters).

### Operational log

- **`events.jsonl`** at `<kb_root>/.kb-mcp/events.jsonl` replaces
  the v25 `fulltext-skips.jsonl`. Six event types:
  - `fulltext_skip` — per-paper fulltext failure (diagnostic)
  - `re_read` — per-paper outcome inside a re-read batch
  - `re_summarize` — per-paper single re-summarize outcome
  - `import_run` — one event per `kb-importer import` run
  - `citations_run` — one per `kb-citations {fetch,link,refresh}`
  - `index_op` — one per `kb-mcp reindex --force` / `snapshot`
- **`kb-mcp report`** (and MCP tool `kb_report`) — 5-section
  digest: `ops`, `skip`, `re_read`, `re_summarize`, `orphans`.
  Live Zotero scan for orphans; events.jsonl aggregation for the
  rest.

### Batch re-read

- **`kb-write re-read`** — batch re-summarize N papers chosen by
  a pluggable selector.
- **Seven selectors**: `unread-first` (default), `random`,
  `stale-first`, `never-summarized`, `oldest-summary-first`,
  `by-tag`, `related-to-recent`.
- **Two sources**: `papers` (default), `storage` (only papers
  with PDF on disk AND imported md).
- Every outcome writes to `events.jsonl`.

### Robustness

- Every selector declares `ACCEPTED_KWARGS`; the CLI warns on
  unknown `--selector-arg` keys so typos don't silently pick
  defaults.
- `oldest-summary-first` parses timestamps as datetime objects
  rather than string-sorting ISO 8601 variants.
- `by-tag` is case-insensitive and accepts `tags=a,b,c` for
  multi-tag OR.
- `related-to-recent`:
  - `anchor_days=abc` now raises `ValueError` instead of silently
    defaulting to 14.
  - SQL seed lookups batch at 400 per query to stay below
    SQLite's IN-variable limit.
  - Unknown fallback selector name now warns on stderr instead
    of silently falling back to random.
- `kb-write re-read --count 0/-1` rejected at the CLI.
- `kb-mcp report --days 0/-1` raises ValueError instead of
  producing a reversed window.
- Semantic Scholar and OpenAlex HTTP clients honour
  `Retry-After` on 429s (capped at 120s).

### Citations

- `kb-citations suggest --min-cites N` — reading-list emitter:
  DOIs cited by many local papers but not in your library.
  Purely local (reads cache, no API).
- `kb-citations refresh-counts` — updates `papers.citation_count`
  via a single paper-meta endpoint per paper (cheaper than
  `fetch`'s full reference walk).

### MCP tools

36 total. New since v25:
- `list_paper_parts`, `kb_report`,
  `find_paper_by_attachment_key`, `top_cited_papers`,
  `paper_citation_stats`, `trace_links`, `dangling_references`,
  `search_papers_graph`, `similar_paper_prior`,
  `refresh_citation_counts`, `link_citations`, `fetch_citations`.

### Install / release hygiene

- `.gitignore` now shipped — protects against accidentally
  committing `__pycache__`, `.env`, local config copies.
- `scripts/check_no_secrets.py` — repeatable pre-release lint
  for API keys, personal info, CJK in code/comments, merge
  conflict markers, home-path leaks.

### Breaking changes from v25

- `zotero-notes/*.md` standalone notes moved to
  `topics/standalone-note/*.md`.
- `skip_log.jsonl` → `events.jsonl` (same schema family,
  richer types).
- `fulltext-skips.jsonl` removed entirely.
- Some orphaned v25 CLI subcommands dropped.
