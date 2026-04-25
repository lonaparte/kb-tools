# Changelog

All notable changes to ee-kb-tools.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is our own (calendar-ish, per-major-iteration).

## [1.4.8] — 2026-04-25

### Fixed (docs / CI)
- **Install order normalised to `kb_core → kb_write → kb_importer →
  kb_mcp → kb_citations`** across all 6 places that document it
  (`scripts/deploy.sh` already correct; `.github/workflows/ci.yml`,
  `DEPLOYMENT.md`, `DEVELOPMENT.md`, `CONTRIBUTING.md` ×2,
  `kb_write/.../prompts/fragments/installation.md` updated). Two
  variants existed: a non-broken-but-different order in CI/CONTRIBUTING
  (kb_mcp before kb_importer) and a *broken* order in
  DEPLOYMENT.md/DEVELOPMENT.md (kb_importer before kb_write — fails
  because kb_importer hard-pins kb_write and pip can't fetch it from
  PyPI). The prompt fragment was also missing kb_core entirely
  (stale since the v27 extraction).

## [1.4.7] — 2026-04-25

### Hardened
- **Static-analysis lints now cover `kb_core`.** `check_no_secrets.py`
  and `check_no_system_paths.py` were both scanning the four
  downstream packages but had skipped `kb_core/` since the v27
  extraction. Adds 10 files to secret scan (170 → 180), 8 to the
  system-path scan (131 → 139). Still clean.
- **`deploy.sh` workspace-parent path canonicalised via
  `CDPATH= cd ... && pwd -P`.** Pre-1.4.7 only stripped a single
  trailing slash; `///`, `//foo//bar`, and symlink chains slipped
  through. `CDPATH=` prefix isolates the `cd` from a user shell's
  CDPATH alias. Existence is checked before canonicalising so a
  bad path errors out at the same boundary as before.

## [1.4.6] — 2026-04-25

### Fixed
- **`kb-citations link` JSONL fallback now atomic.** When DB write
  fails and `fallback_jsonl=True`, citation-edges.jsonl is written
  via tempfile + `os.replace`. Pre-1.4.6 a mid-loop interruption
  left a half-populated file. Inlined helper `_atomic_write_text`
  in linker.py (kb_citations doesn't dep on kb_write); 5 new tests.

## [1.4.5] — 2026-04-25

### Fixed
- **kb-citations YAML config no longer silently shadowed by CLI defaults.**
  `--max-refs`, `--max-cites`, `--freshness-days` now default to `None`;
  `_build_ctx` distinguishes "flag not passed" from explicit values, so
  YAML keys (`max_refs`, `max_cites`, `freshness_days`, `fetch_citations`)
  apply when the CLI is silent. `freshness_days: 0` correctly maps to
  "force refetch" (None downstream) instead of being shadowed by 30.
- **`--with-citations` now uses `argparse.BooleanOptionalAction`** so
  `--no-with-citations` can override `fetch_citations: true` from YAML.
- 6 new precedence tests in `test_citations_yaml_precedence.py`.

### Refactored (slimming, -135 lines net)
- `commit_staged()` requires `files=` (the legacy `None`/whole-index
  branch had no production callers; 2 stale tests dropped).
- `_nullcontext` class replaced with stdlib `contextlib.nullcontext`.

## [1.4.4] — 2026-04-25

Modest slimming pass — consolidate a handful of duplicated helpers
that had drifted into 4 near-identical copies, drop one dead helper
left behind by 1.4.2's `yaml.safe_dump` switch. Total: -58 lines
across the toolchain (32653 → 32595). No behaviour change.

### Refactored

- **`positive_int` / `nonnegative_int` consolidated to
  `kb_core.argtypes`.** The `_positive_int` argparse helper had
  identical 13-line copies in `kb_write/commands/_shared.py`,
  `kb_mcp/server_cli.py`, `kb_importer/commands/_shared.py`, and
  `kb_citations/cli.py`. `_nonnegative_int` had two copies (kb-importer
  + kb-citations). Now defined once in `kb_core.argtypes`; each
  CLI re-imports under the underscore-prefixed local name so existing
  call sites are unchanged. The shim/identity test
  (`scripts/check_package_consistency.py`) still passes since the
  duplicates are now the same object across packages.

- **Dead `_yaml_escape()` removed from `kb_importer/longform.py`.**
  Was the legacy hand-rolled escape used before 1.4.2 switched to
  `yaml.safe_dump`. The function survived as unused code; only
  reference remaining was a historical-context comment, which now
  reads as historical. The duplicate copy in
  `kb_write/ops/migrate_chapters.py:442` is still live (used by the
  one-shot title-rename migration) and kept as-is.

### Verification

- 550 unit tests pass (stdlib runner + real pytest).
- 46 e2e tests pass.
- All 5 lints green: doc-sync, package-consistency, no-secrets,
  cross-module-imports, no-system-paths.
- The 4 cross-package `_positive_int` references are now `is`-identical
  to `kb_core.argtypes.positive_int`; same for the 2 `_nonnegative_int`
  references.

## [1.4.3] — 2026-04-25

Audit-response wave 4: convert four 1.4.2 "warning + continue"
softenings into hard refusals, and tighten CI supply-chain. Same
day as 1.4.2 because the audit pointed out — correctly — that
documentation alone is not a control surface for an agent
toolchain. The fix shape across all four items: refuse by default,
opt-in via an env var that a human can read about but an agent
won't guess.

### Hardened (security)

- **kb-mcp PATH fallback default-deny.** 1.4.2's resolution chain
  ended at `shutil.which("kb-mcp")` with an INFO-level "verify
  this is the binary you expect" message and ran it anyway. PATH
  is precisely the attack surface the wave-1 fix tried to escape;
  "log and continue" still hands control to the suspicious
  binary. 1.4.3 only consults PATH if `KB_WRITE_ALLOW_PATH_KB_MCP=1`
  is in the environment. Without the opt-in, kb-mcp resolution
  fails closed (skips reindex with a debug-level hint at the
  opt-in env var). Resolved PATH binary is still WARNING-logged
  at the absolute path even with opt-in, and `os.path.abspath()`'d
  so a relative PATH entry can't redirect via cwd change.

- **`--no-lock` / `--no-git-commit` require explicit opt-in.**
  These flags exist for human-driven debugging — they switch off
  concurrent-write safety and git-as-source-of-truth respectively.
  1.4.2 protected them only via the write_workflow.md fragment
  ("agents must not use without human approval"). Documentation
  is not a technical control. 1.4.3 adds `_check_unsafe_flags()`
  to both `kb-write` and `kb-importer` CLIs: any run combining
  these flags with no `KB_WRITE_ALLOW_UNSAFE_FLAGS=1` in the
  environment exits 2 with a message pointing at the opt-in. The
  env var name is shared across both tools (single
  `export KB_WRITE_ALLOW_UNSAFE_FLAGS=1` covers a debugging
  session that hits both). `--no-reindex` is intentionally NOT
  gated — stale search is recoverable; the others aren't.

- **GitHub Actions pinned to commit SHAs.** Workflow previously
  used version-tag refs (checkout v4, setup-python v5, cache v4,
  upload-artifact v4) — refs the action owner can overwrite at
  any point. 1.4.3 pins each step to its release commit SHA with
  the resolved version recorded in a trailing comment for
  Dependabot review. The current CI doesn't carry secrets, but
  the practice should predate any future addition.

- **Secret scanner extended to `.github/` and root metadata.**
  `check_no_secrets.py` previously skipped workflow YAMLs and
  root release-note files. 1.4.3 adds `.github/` to SCAN_DIRS
  and `CHANGELOG.md` / `UPGRADING.md` to the root file list,
  with both changelog files added to the CJK-exempt set
  (release notes legitimately quote user feedback verbatim).
  One historical home-path placeholder in a 0.29.4 release note
  replaced with generic phrasing.

### Refactored

- **`kb_write/safety.py` and `kb_importer/safety.py` extracted.**
  The unsafe-flag gate function and its env-var sentinel moved
  to standalone modules so unit tests can import them without
  pulling in `frontmatter`, `sqlite-vec`, or the rest of the
  command tree. cli.py re-exports both names so the public
  surface is unchanged. Same shape as `kb_write/audit.py`,
  `kb_write/zones.py` etc. — small, focused, dependency-free.

### Verification

- 550 unit tests pass (was 539; +11 new tests covering the gates
  and PATH default-deny). Stdlib-runner + real-pytest both green.
- 46 e2e tests pass.
- Secret scanner clean over 168 source files (was 167 — added
  `.github/workflows/ci.yml`).
- post_install_test.py updated to inject `KB_WRITE_ALLOW_UNSAFE_FLAGS=1`
  in its `cli()` helper since the smoke test deliberately uses
  `--no-git-commit --no-reindex` to skip git/reindex setup.

## [1.4.2] — 2026-04-25

Security & robustness patch. Reviewer audited the orchestration
layer (deploy, locks, atomic writes, git, kb-write ops, kb-mcp
write tools) and surfaced 16 findings across two passes. Most are
defense-in-depth — the codebase already had above-average security
posture (list-form subprocess, safe_resolve path bounds, atomic
write + mtime guard, secret-policy in scaffolds). The fixes here
close two ACTUAL exposure paths (PATH-shadowed kb-mcp + delete
committing whole index) and harden a dozen smaller surfaces.

### Fixed (high risk)

- **kb-mcp resolved by absolute path, not bare PATH.**
  `kb_write/reindex.py:trigger_reindex` previously did
  `subprocess.run(["kb-mcp", ...])`, trusting whatever PATH
  resolution kicked in. A user with a writeable directory
  prepended to PATH (or a malicious binary in `~/bin/`) would
  have every kb-write call silently execute that binary post-
  write. New `_resolve_kb_mcp(kb_root)` tries, in order:
  `<workspace>/.ee-kb-tools/.venv/bin/kb-mcp` (deploy.sh layout),
  `<sys.executable's dir>/kb-mcp` (developer venv), then
  `shutil.which()` with an INFO-level log of the absolute path
  so a suspicious resolution is at least visible. The subprocess
  is invoked with the absolute path so PATH mutation mid-run
  can't redirect it.

- **`kb-write delete` no longer commits the whole git index.**
  The `delete` op stages its removal via `git rm`, then called
  `commit_staged()` with no pathspec. Pre-1.4.2 that committed
  whatever else happened to be staged (user-staged unrelated
  changes, sibling-process stages). Now passes
  `files=[address.md_rel_path]` so only the deletion lands in
  the commit. `commit_staged()` gained a `files=` parameter for
  this; legacy callers passing None retain the historical
  whole-index behavior.

- **git invocations disable hooks by default.** New `_git_argv()`
  helper prepends `-c core.hooksPath=<null-device>` to every git
  command except read-only `is_git_repo()` / `rev-parse`. Hooks
  are arbitrary code; a KB cloned from an untrusted source, or
  shared with collaborators who've added project-specific hooks,
  shouldn't have those hooks run silently on every kb-write
  auto-commit. `auto_commit()` and `commit_staged()` accept
  `run_hooks=True` to re-enable per-call.

- **`import_lock.py` no longer unlinks lock file on success.**
  Pre-1.4.2 the success path released the flock and unlinked
  the file, opening a race window where two processes could
  both believe they hold the lock (one flocked the just-deleted
  inode, the other created a fresh inode). `kb_write/atomic.py`
  already had a long warning comment about this; import_lock
  now follows the same rule. Tradeoff: a small empty file
  remains on disk after release. Accepted.

- **`kb_importer/commands/import_pipeline.py`** missing
  `from pathlib import Path` import. Used only in type-hints
  today (caught by `from __future__ import annotations`), so
  no current runtime impact, but a future
  `isinstance(x, Path)` check would NameError. Added.

### Fixed (medium risk)

- **`post_install_test.py` cleanup uses tracked paths + content
  marker, not glob.** Pre-1.4.2 cleanup deleted any file matching
  `thoughts/*post-install-smoke-test*.md` — would unintentionally
  delete user-authored thoughts that happened to share that
  substring. Now records exact paths during creation, embeds a
  per-run UUID marker in the body, and on cleanup verifies BOTH
  conditions before deletion. Refuses to delete with a warning
  if the file at a tracked path no longer carries the marker.

- **`init.py --refresh` now passes `expected_mtime` to
  `atomic_write`.** Closes a TOCTOU window between the refresh
  read and the rendered-merge write. Other init paths and all
  thought / topic / preference ops already had mtime guards;
  refresh was the one omission.

- **`init.py` refuses to scaffold into a non-empty non-KB dir
  unless --force.** Prevents the typo'd `--kb-root ~` /
  `--kb-root /tmp` accident. Heuristic: existing dir + has
  children + none of those children look like KB markers
  (`papers/`, `thoughts/`, `.kb-mcp/`, `CLAUDE.md`, etc.) →
  raise `InitNonEmptyDirError`. `--force` bypasses.

- **`deploy.sh` refuses `WORKSPACE_PARENT="/"`.** Pre-1.4.2 a
  bare `/` would create `/.ee-kb-tools/`. Confusing rather than
  destructive (cleanup is scoped), but worth blocking outright.

- **`longform.py` chapter md frontmatter built via `yaml.safe_dump`.**
  Pre-1.4.2 the frontmatter was hand-concatenated f-strings and
  only `title` went through `_yaml_escape`. Today's Zotero keys
  are `[A-Z0-9]{8}` so safe in practice, but a chapter title
  containing `\n` / `"` / `:` would silently produce malformed
  YAML — which the next read would interpret as "no frontmatter",
  flag the chapter as unprocessed, and re-summarize on every
  run, burning LLM tokens. `safe_dump` handles all shapes
  correctly by construction.

### Fixed (low risk / hygiene)

- **`git.py` SHA lookup uses pathspec-scoped `git log -1
  --format=%H` when `files` is set.** Pre-1.4.2 it ran
  `rev-parse HEAD` after the commit, which under high
  concurrency could pick up a sibling commit's SHA. Now
  `git log -1 --format=%H -- <pathspec>` returns the most
  recent commit that touched OUR file specifically.

- **`audit.py` truncates caller-supplied `note` and string
  values in `extra` to 1000 chars, with a warning if the encoded
  line still exceeds PIPE_BUF (4096B).** Honors the docstring's
  atomicity claim for single-write append. `events.jsonl`
  already truncates `detail` to 500B; audit now mirrors.

- **`migrate_slugs.py` docstring** corrected to reflect actual
  implementation (uses `shutil.move` + git's heuristic rename
  detection on follow-up `git add`, NOT `git mv`).

### Workflow + scaffold hardening

- **kb-mcp.yaml scaffold + `OpenAIEmbeddingProvider`** warn
  loudly when `openai_base_url` points at a non-official,
  non-localhost host. Doesn't refuse — DashScope, Azure,
  self-hosted public gateways are legit — but a tampered config
  pointing at an attacker host now logs a WARNING with the
  exact URL on every embedding call.

- **`write_workflow.md` fragment** grew a "Flags an agent must
  NOT use without explicit human approval" section listing
  `--no-lock`, `--no-git-commit`, `--no-reindex`, raw git
  commands targeting KB files, and `*_base_url` overrides.
  This propagates into every workspace's `CLAUDE.md` /
  `AGENTS.md` / `README.md` after `kb-write init --refresh`.

### Deferred

- **Windows write-lock fallback PID-file race** (audit A.5).
  POSIX path uses `fcntl.flock` correctly; Windows still uses
  the older PID-file approach with a known race. No Windows
  users in scope; revisit if/when one shows up.

### Test coverage

- `test_security_wave1.py` — 9 new cases: kb-mcp resolution
  prefers workspace venv over PATH, returns absolute path, None
  when absent; `_git_argv` includes hooks override by default
  and omits with `run_hooks=True`; `auto_commit` end-to-end
  passes hooks-disabled; `commit_staged` scopes to pathspec or
  falls through; `import_lock` keeps lock file (truncated) on
  release.
- Updated `test_import_lock.py` and `test_auto_commit_pathspec.py`
  to match the new behaviors.

### Verification

- All four lints + docs-sync gate clean.
- 539/539 unit tests via stdlib runner; same via real pytest
  (CI workflow exercises both).
- 46/46 e2e.
- post-install smoke 14 pass / 2 expected skip.
- `pre_release_full_check.sh` green end-to-end.

## [1.4.1] — 2026-04-25

CI + quality infrastructure. Reviewer flagged that 1.4.0's release
gate is 100% manual: `pre_release_full_check.sh` is comprehensive
but it depends on a maintainer running it. Also flagged: stdlib
test-runner shim might drift from real pytest, provider mock
coverage is concentrated on happy-path-adjacent failure modes,
and README occasionally lags CHANGELOG.

All four addressed here. No behavior changes — runtime is identical
to 1.4.0 for any user-visible code path.

### Added

- **`.github/workflows/ci.yml`.** Runs on push to main + on every
  pull request. Matrix Python 3.10 + 3.13. Steps mirror
  `pre_release_full_check.sh`'s lint + byte-compile + unit + e2e
  gates, plus a real-`pytest` step (next item) and the doc-sync
  gate. Separate `release-zip` job runs after `checks` passes,
  produces `kb-tools-VERSION.zip` as a 14-day artefact. The
  release-gate is now permanently visible on GitHub for every
  commit, not just at release time.

- **Real-pytest CI step.** `pytest tests/unit/ -q` runs alongside
  the stdlib `scripts/run_unit_tests.py`. Both must pass. Catches
  shim-vs-pytest drift (we already hit two: `pytest.approx` in
  1.3.1, `capsys` in 1.4.0). 530/530 tests pass under both.

- **`scripts/check_docs_sync.py`.** Lightweight regex gate:
    - VERSION must have a matching `## [X.Y.Z]` section in
      CHANGELOG.md (the most common drift mode).
    - README.md must mention the current major.minor (forces a
      surface-area touch on minor bumps without demanding parity
      on every patch).
    - If `kb_core.schema.SCHEMA_VERSION` exists, UPGRADING.md
      must mention `vN` for the current schema number.
  Wired into both `pre_release_full_check.sh` and
  `make_release.sh`'s pre-flight, plus the new CI workflow.

- **`tests/unit/test_summarize_edge_cases.py`.** 10 new cases
  extending provider-mock coverage:
    - 401 / 403 (auth / forbidden) → SummarizerError, no retry,
      not BadRequestError.
    - 200 + non-JSON body → BadRequestError ("non-JSON" message).
    - 200 + valid JSON missing `choices[0].message.content` →
      SummarizerError with diagnostic message.
    - 429 with OpenRouter upstream-wrapped body shape → quota
      classified, Retry-After honored.
    - 429 with no header AND no body → quota_type=unknown,
      retry_after=None (caller's default sleep applies).
    - Gemini RESOURCE_EXHAUSTED without per_day/per_minute
      → quota_type=unknown.
    - DeepSeek 400 "model not supported" → BadRequestError, NOT
      QuotaExhaustedError.
    - URLError(socket.timeout) → still hits the retry path.
    - Gemini 400 "Budget 0 is invalid" (real-world thinking-budget
      issue) → BadRequestError, no retry.

### Changed

- **README.md** introduces an explicit "Current release: 1.4.x"
  callout above the doc index, satisfying the new doc-sync gate
  and giving a single line that future releases can update with a
  semver edit.

### Verification

- All four lints + new docs-sync gate clean.
- Byte-compile clean.
- 530/530 unit tests via stdlib runner; **same 530/530 via real
  pytest** (114 deprecation warnings, all in third-party libs:
  python-frontmatter using codecs.open on py3.14, google-genai's
  _UnionGenericAlias). No drift.
- 46/46 e2e.
- `pre_release_full_check.sh` green end-to-end.

## [1.4.0] — 2026-04-24

Minor release. Closes the three LLM-unavailable robustness items
that 1.3.1 deliberately deferred:

1. Circuit breaker for batch LLM loops.
2. `kb-importer preflight` subcommand.
3. Non-Gemini quota classification.

After 1.4.0 the toolchain's behavior against a flaky / misconfigured
/ regionally-down LLM provider is: per-call retry (1.3.1) →
fallback-model switch (existing, Gemini-only) → batch-level circuit
breaker (new) → `events.jsonl` classification (existing) →
continue-to-next-paper. Nothing surprising should reach
"silently burns budget across 1000 papers".

### Added

- **`kb-importer preflight` subcommand.** Sends a single
  ~5-token request to the configured fulltext provider / model
  to verify key + endpoint + model work BEFORE a real import
  spends real tokens. Safe to run unlimited times. Structured
  exit codes: 0 ok, 2 key missing, 3 quota, 4 bad request, 5
  other LLM error. Bypasses `load_config()` — doesn't require
  library_id / zotero_storage to be set (you can preflight
  before your Zotero setup is complete).

- **Circuit breaker in batch fulltext + re-read loops.** Tracks
  the last N error codes in a sliding window; trips if all N
  are the same breaker-relevant code (`llm_bad_request`,
  `llm_other`, `other`). Success resets the streak. Local
  per-paper issues (`pdf_missing`, `pdf_unreadable`,
  `already_processed`, `bad_target`, `mtime_conflict`,
  `quota_exhausted`) NEVER trip it — those have their own
  dispositions. Configurable via `--max-consecutive-failures N`
  (default 5; 0 disables). Available on `kb-importer import`
  and `kb-write re-read`.

- **Non-Gemini quota classification.** OpenAI / DeepSeek /
  OpenRouter HTTP 429 + HTTP 402 (insufficient_quota) now raise
  `QuotaExhaustedError` with classified `quota_type` and parsed
  `retry_after`, matching the Gemini path's contract.
  `_classify_quota_kind()` recognizes OpenAI error types
  (`insufficient_quota` → daily, `rate_limit_exceeded` → rate)
  in addition to the Gemini English phrasings.
  `_extract_retry_after()` prefers the HTTP `Retry-After`
  header, falls back to body-text parsing for both Gemini's
  `retryDelay: "34s"` and OpenAI's `Please try again in 20ms`
  shapes (ms→s conversion included).

- **HTTP 404 on OpenAI-compatible chat** now routes through
  `BadRequestError` (was generic `SummarizerError` before). 404
  on a chat completion means model-name is wrong or endpoint
  doesn't exist — deterministic, never retry. Matches Gemini
  provider's 404 handling.

### Test coverage

- `test_circuit_breaker.py` — 7 cases covering disabled mode,
  mixed-code non-trip, success-resets-streak, irrelevant-code
  ignored, window-of-1 trips immediately, tripped state is sticky.
- `test_quota_classification.py` — 11 cases covering all classifier
  shapes (OpenAI insufficient_quota, rate_limit_exceeded, Gemini
  English, unknown), retry_after header-vs-body priority, OpenAI
  `ms` / `s` shapes, and end-to-end OpenAIChatProvider 429 / 402
  / 404 routing.
- `test_preflight_cmd.py` — 4 cases covering missing-key exit 2,
  happy path exit 0, quota exit 3, bad-request exit 4.

### Verification

- Four lints + byte-compile clean across all 5 src/ trees.
- **520/520 unit tests** (was 497 in 1.3.1; +23 from the three
  new test files: 7 breaker, 11 quota, 4 preflight; plus 1
  small adjustment in the existing chat retry test for the
  new 404 branch).
- 46/46 e2e.
- post-install smoke: 14 pass / 2 expected skip.
- `pre_release_full_check.sh` green end-to-end.
- Release zip kb-tools-1.4.0.zip.
- Fresh-venv pip-wheel install: `kb-importer preflight --help`
  lists the new subcommand; `kb-write re-read --help` shows
  `--max-consecutive-failures`; `kb-importer import papers
  --help` shows same flag.

## [1.3.1] — 2026-04-24

Robustness polish. Two reviewer-caught gaps in config parsing and
LLM transport-error handling. No behavior changes for valid configs
or stable networks — only the two error paths now produce actionable
errors / retry transparently instead of bare Python tracebacks.

### Fixed

- **kb-mcp config nested sections must be mappings.** Pre-fix,
  `kb_mcp.config.load_config()` validated the top-level YAML was a
  mapping but then called `.get(...)` on `logging` / `embeddings` /
  `store` without checking THEIR types. If a user wrote
  `embeddings: false`, `logging: debug`, or
  `store: [- journal_mode: wal]` (common YAML indentation mistakes),
  load_config crashed with `AttributeError: 'bool' object has no
  attribute 'get'` — not a diagnosis the user could act on. Added
  `_mapping_section()` helper that raises ConfigError with a
  "check YAML indentation" pointer, matching the strictness the
  top-level YAML check already had.

- **Transient-transport retry in the summarizer.** `summarize.py`'s
  stdlib-urllib HTTP paths (OpenAI-compatible chat via
  `OpenAIChatProvider`, Google Gemini via `GeminiProvider`)
  previously failed hard on any transient error — a single DNS
  hiccup, connection reset, or HTTP 5xx would surface as
  `SummarizerError` and the paper got classified as skip. Over a
  1000-paper batch this adds up.

  Added a conservative retry loop: up to 2 retries on
  `urllib.error.URLError`, `TimeoutError`, and HTTP 5xx, with
  exponential backoff (1s, 3s). HTTP 400/404 (deterministic input
  errors) never retry. HTTP 429 goes through the existing
  quota-classification path for Gemini — retry logic there is
  already correct via `QuotaExhaustedError.retry_after`.

  The OpenAI and Gemini Python SDKs handle this internally; this
  change only matters for the stdlib-urllib paths (which kb-importer
  uses for all three providers: Gemini, OpenAI-compatible, OpenRouter).

### Added (test coverage)

- `test_config_section_types.py` — 8 cases: mapping pass-through,
  bool / str / list / int sections rejected with ConfigError, plus
  end-to-end `load_config` verification that `embeddings: false`
  fails cleanly.
- `test_summarize_retry.py` — 9 cases monkeypatching `urllib.request
  .urlopen` with scripted outcomes: happy path, URLError retried
  to success, TimeoutError retried, HTTP 503 retried, HTTP 400/404
  NOT retried, retry budget exhausted → SummarizerError, Gemini 429
  bails via QuotaExhaustedError without consuming retry budget.

### Audit notes (1.3.1 context)

The reviewer asked about robustness when the LLM API is unavailable.
Summary of what's already in place plus what this release adds:

- **Missing API key** → `build_from_config` returns None (embeddings)
  / `SummarizerError` raised early (fulltext). Graceful degrade for
  embeddings; hard-fail for fulltext (correct — user explicitly asked
  to summarize).
- **Per-batch embedding failure** → `log.warning`, loop continues,
  partial papers flagged so next `index` run retries. Already correct.
- **Per-paper fulltext failure** → classified into events.jsonl
  (`quota_exhausted`, `llm_bad_request`, `pdf_missing`, `llm_other`),
  batch continues, `kb-mcp report` aggregates. Already correct.
- **Gemini RPD quota** → `--fulltext-fallback-model` auto-switch.
  Already correct.
- **Gemini RPM quota** → `time.sleep(retry_after)` then retry same
  model. Already correct.
- **Transient network / 5xx** → 1.3.1: exponential-backoff retry,
  up to 2 retries, across both stdlib-urllib paths.

Not implemented in 1.3.1 (deferred):
- **Circuit breaker** for N-consecutive same-code failures in
  batch fulltext / re-read. The existing per-paper retry + typed
  classification already handles most cases; adding a circuit
  breaker is a 1.4 feature if needed.
- **Preflight connectivity check** (`kb-importer preflight
  --fulltext-provider X` pings with a 5-token request). Nice-to-have,
  low priority since the first paper's error message is already
  actionable.
- **Non-Gemini quota classification.** OpenAI 429 / OpenRouter 429
  currently hit the generic 5xx retry path, which is suboptimal but
  not broken — the retry logic will eventually give up and the
  paper will be skipped. Proper classification needs provider-
  specific error-body parsing; deferred.

### Verification

- Four lints + byte-compile clean.
- 497/497 unit tests (+17 from 1.3.0: 8 config-section-type,
  9 summarizer-retry).
- 46/46 e2e.
- post-install smoke: 14 pass / 2 expected skip.
- Release zip kb-tools-1.3.1.zip.

## [1.3.0] — 2026-04-24

Minor release. Introduces **three integration modes for re-read /
re-summarize** (`--mode {append,replace,merge}`), replacing the
pre-1.3 single-mode judge-based merge. The new default mode is
`append`, which preserves the original fulltext baseline and stacks
timestamped revisit blocks in a new `## Revisits` region — safe to
run repeatedly with cheap / free-tier models without eroding the
authoritative first-pass summary.

Rationale + design trade-offs are discussed inline; short version:
the judge LLM in the pre-1.3 merge path was the most brittle link,
especially once `--fulltext-provider openrouter` (1.2.0) made it
easy to run rewrite on a weak model. The append mode bypasses the
judge entirely; the `replace` mode skips it too; the `merge` mode
is retained but gains `--judge-provider` / `--judge-model` flags so
users can explicitly pay for quality on the judge pass.

### Added

- **`kb-write re-summarize --mode {append,replace,merge}`.** Default
  is `append`. `re-read` accepts the same flag and passes it
  through. Detailed semantics in the CLI `--help`:
    - **append**: prepend a new `<!-- kb-revisit-block -->` at the
      top of the paper md's `## Revisits` region (newest first).
      Baseline `<!-- kb-fulltext-* -->` region NEVER changes. No
      judge LLM call. Safe to run repeatedly. Each block carries the
      run's date and model identifier.
    - **replace**: overwrite the fulltext region with a fresh
      7-section summary. Revisits region (if any) untouched. No
      judge LLM. Equivalent to a single-paper
      `kb-importer import papers --force-fulltext KEY`.
    - **merge**: pre-1.3 behavior — per-section LLM judge decides
      old vs new, only `verdict=new` sections get spliced in.

- **`--judge-provider` / `--judge-model`** on re-summarize and
  re-read. Only consulted in merge mode. Lets users run rewrite on
  a cheap model and judge on a stronger one (recommended when
  `--model` is a free-tier catalog entry like
  `openai/gpt-oss-120b:free`).

- **`## Revisits` region markers** in md files:
    - `<!-- kb-revisits-start -->` / `<!-- kb-revisits-end -->`:
      whole region bounds; surgical splice point for append mode.
    - `<!-- kb-revisit-block date="…" model="…" -->` /
      `<!-- /kb-revisit-block -->`: per-entry bounds for the
      doctor + any future per-revisit indexing.

- **kb-write doctor** now verifies Revisits markers are paired and
  balanced. Half-open regions surface as `category=revisits`
  error findings.

- **Unit-test coverage**: `test_revisits_modes.py`,
  `test_revisits_preserved.py`, `test_revisits_doctor.py`
  (23 cases total).

### Changed

- **kb-importer preserved-regions now includes the Revisits
  section.** Without this change, `kb-importer sync KEY` or
  `--force-fulltext KEY` on a paper with revisits would silently
  drop the entire section on re-render. New `PreservedContent
  .revisits_section` field holds the verbatim `## Revisits`
  region across re-imports.

- **`re_summarize.ReSummarizeReport`** gains four fields:
  `mode`, `revisit_date`, `model_used`, with `summary_line()`
  and `format_report()` producing mode-appropriate output.
  Pre-1.3 callers that read `report.verdicts` keep working —
  in append / replace modes the list is simply empty.

- **No schema bump.** `## Revisits` content is indexed by kb-mcp as
  regular paper body text (FTS5 + vectors via the existing chunking
  path); no `kind=revisit` or schema migration. Users can search
  revisits as normal paper content. First-class revisit indexing
  (`list_revisits` tool / separate chunk kind) was considered and
  deferred — see design notes in the PR.

### Notes on interaction with earlier features

- The OpenRouter default fulltext model
  (`openai/gpt-oss-120b:free`, 1.2.1) combined with `--mode append`
  (1.3.0) gives you a risk-free path to experiment with cheap
  models: every re-read is additive, nothing destructive. The
  original paid-model baseline (written by `kb-importer --fulltext`)
  stays authoritative; revisit blocks accumulate as a historical
  record.

- `kb-mcp reindex --force` is NOT required for 1.2.1 → 1.3.0.
  Schema unchanged. The only workflow change a user might want is
  to pick up revisits in the FTS / vector index, which happens on
  the next `kb-mcp index` run automatically.

### Verification

- Four lints + byte-compile clean.
- 480/480 unit tests (was 457; +23 covers the three new test files:
  13 for marker / prepend / format logic, 5 for preserved round-
  trip in kb_importer, 6 for doctor detection of malformed
  regions).
- 46/46 e2e.
- post-install smoke: 14 pass / 2 expected skip (no OpenAI key /
  S2 rate-limit).
- `pre_release_full_check.sh` green end-to-end; release zip builds
  clean.

## [1.2.1] — 2026-04-24

Merge of PR #1 (claude-code-agent robustness improvements) plus two
rounds of review fixup. Ships stricter config validation, better
embedding-response error handling, and four reviewer-caught
consistency bugs around the embedding `dim` override and related
diagnostics.

### Added (from PR #1 base)

- **Config-load validation for `logging.level` and
  `embeddings.batch_size`.** Invalid values are now caught at config
  load time with ConfigError rather than silently normalized
  (log level) or silently passing (batch size) and failing later.
- **API-response parse wrapping on OpenAI and Gemini embedding
  providers.** Malformed responses surface as `EmbeddingError`
  instead of bare `AttributeError` / `TypeError`.

### Changed

- **`--fulltext-provider openrouter` default model changed** from
  `openai/gpt-4o-mini` to `openai/gpt-oss-120b:free`. The new
  default costs $0, which matches the OpenRouter "pick any model"
  appeal. Downside: free-tier open-weight models are meaningfully
  less capable than paid GPT-4-class models; `--help`, the README
  provider table, and the CHANGELOG all point this out and
  suggest upgrade candidates (`google/gemini-2.5-flash` for cheap
  paid, `anthropic/claude-sonnet-4.5` for quality). The old
  default (`openai/gpt-4o-mini`) remains available as an explicit
  override.

### Changed (fixup pass 1)

- **Log level validation accepts `fatal` / `warn` aliases.** Python's
  `logging` module defines these as aliases for CRITICAL / WARNING;
  the original PR's whitelist rejected them, which would have been a
  regression from the pre-validation `getattr(logging, NAME.upper(),
  INFO)` behavior. Aliases are now normalized to their canonical
  names; the downstream code only sees the five canonical strings.

- **Batch size over-limit raises `ConfigError` instead of silently
  capping.** Silently truncating `batch_size=5000` to 2048 would
  leave the user wondering at runtime why indexing is slower than
  configured. A ConfigError at load time forces an explicit
  decision: lower the config or switch provider.

- **Unknown-provider batch sizes pass through unchanged.** The
  original PR capped unknown providers to 100, which broke
  kb-mcp.yaml's documented self-hosted endpoint use cases (Ollama /
  vLLM / LocalAI / DashScope reached via `openai_base_url`). Those
  gateways can handle much larger batches; we can't know their
  limits, so the user's config is honored.

- **`test_embedding_response_robustness.py` rewritten** to actually
  exercise the production `embed()` path via client-monkeypatch.
  The original tests ran an inline try/except in the test body, so
  they passed whether or not the production code had the wrap — a
  regression magnet.

### Fixed (fixup pass 2 — reviewer audit on top of the PR)

- **`openai` branch of `build_from_config()` now forwards
  `cfg.embedding_dim` to `OpenAIEmbeddingProvider`.** The scaffold-
  documented self-hosted-gateway examples (Ollama
  `nomic-embed-text` + `dim: 768`; vLLM `BAAI/bge-large-en-v1.5` +
  `dim: 1024`) were documented-but-not-working — `dim` was set in
  YAML but never passed through, so `_model_dim(model)` fired on
  the unknown name and construction failed. Now wired through.

- **`embeddings.dim` strict-parsed via `_parse_positive_int`.**
  Previously a bare `emb_cfg.get("dim")` — bool / float / zero /
  negative / bad-string values slipped through and failed later
  with confusing sqlite-vec errors. Now rejected at load time with
  a clear ConfigError, matching the strictness already applied to
  `batch_size`.

- **Vector dimension mismatch now produces a helpful error** before
  the SQLite INSERT. `run_embedding_pass` checks `len(vec) ==
  store.vec_dim` and, on mismatch, raises ValueError naming the
  fix: set `embeddings.dim: <old>` to restore the previous model,
  or `kb-mcp reindex --force --dim <new>` to rebuild at the new
  dim. Previously the user saw sqlite-vec's terse
  "vec_f32(X) needs N bytes, got M" with no hint that the root
  cause was a model / provider switch.

- **`kb_importer/pyproject.toml` description** updated from
  "via the Zotero local API" to "via Zotero web or local APIs".
  Stale since the 0.28.0 flip of `source_mode` default from `live`
  to `web`.

- **Third-party dependency pins reverted to semantic minimums.**
  Prior version-bump sweeps had accidentally dragged
  `python-frontmatter`, `mcp`, and `openai` version pins along
  with the ee-kb version (so 1.2.0 had `openai>=1.2.0` etc.). These
  third-party libraries have independent release cycles and should
  never have been coupled to the ee-kb version. Restored to their
  originally-intended `>=1.0.0` minimums.

### Added (test coverage)

- `test_embedding_dim_override.py` — 4 cases locking in the scaffold-
  documented `dim:` override path: unknown model + explicit dim
  works, unknown model + no dim fails, known model + no dim uses
  the built-in table, known model + override wins.
- `test_embedding_dim_mismatch.py` — 2 cases: the error-path message
  exercise, plus a static-code guard that the check can't be
  silently removed.
- `TestEmbeddingDimStrictParsing` class in
  `test_config_validation.py` — 6 cases verifying bool / float /
  zero / negative / bad-string all fail with ConfigError.
- `test_config_validation.py` updated for aliases + ConfigError
  batch-size path + unknown-provider pass-through.
- `test_embedding_response_robustness.py` rewritten (see above).

### Verification

- Four lints + byte-compile on all five src trees: clean.
- Unit tests: 457/457 (PR's base adds 14 → 1.2.0 had 425 → PR raises
  to 439 → my fixup pass 1 adjusts to 445 → fixup pass 2 adds 12 new
  → final 457).
- E2E: 46/46.
- post-install smoke: 14 pass / 2 expected skip (no OpenAI key / S2
  rate-limited).
- `pre_release_full_check.sh` green end-to-end including release zip.

## [1.2.0] — 2026-04-24

Extends OpenRouter support from the embedding pipeline (1.1.0) to
also cover the fulltext summarization pipeline — so users can pick
any OpenRouter-catalog model (anthropic/claude, google/gemini,
deepseek/deepseek-chat, etc.) for paper summaries without managing
per-provider accounts. Also splits the two OpenRouter env vars so
embedding and fulltext can point at different OpenRouter accounts.

### Added

- **`kb-importer --fulltext-provider openrouter`** — new choice in
  the fulltext summarizer. Default model `openai/gpt-4o-mini`;
  override with `--fulltext-model <any catalog id>`. Examples in
  `--help` and kb_importer/README.md: `openai/gpt-4o`,
  `google/gemini-2.5-flash`, `anthropic/claude-sonnet-4.5`,
  `deepseek/deepseek-chat`. Reads `OPENROUTER_API_KEY`.

- **Optional `extra_headers` on `OpenAIChatProvider`** — lets the
  OpenRouter branch set `HTTP-Referer` and `X-Title` headers per
  OpenRouter docs (ee-kb-tools attribution on their public
  leaderboard; safe to omit; doesn't affect routing).

- **Env-var split for OpenRouter** between the two pipelines:
  - `OPENROUTER_EMBEDDING_API_KEY` — kb-mcp embedding (RAG).
  - `OPENROUTER_API_KEY` — kb-importer fulltext summary.

  The two pipelines pick these up by default via their respective
  config defaults (`openrouter_api_key_env` in kb-mcp.yaml).
  **Single-key convenience**: if `OPENROUTER_EMBEDDING_API_KEY` is
  unset but `OPENROUTER_API_KEY` is, the embedding provider
  transparently falls back — one key works for both pipelines.
  Users who explicitly set `openrouter_api_key_env` to a custom
  value opt out of the fallback. Logged at INFO so the behavior
  is visible.

### Changed

- Root README's "Two LLM configurations" table now lists the env
  var names per provider so the separation (and the OpenRouter
  split) is visible at a glance.

- `kb_importer.yaml` scaffold grew a pointer block documenting the
  four fulltext providers and their env vars, plus an explicit
  note that the RAG embedding pipeline is configured elsewhere.

- `kb_mcp.yaml` scaffold documents the env-var split + single-key
  fallback with an inline explanation.

### Verification

- Lints clean; byte-compile clean on all five packages.
- 425/425 unit tests (+10 over 1.1.0: 7 new
  `test_summarize_openrouter` cases covering factory dispatch,
  missing-key error, no-leak-from-embedding-env, base URL,
  attribution headers, unknown-provider message; +3 new
  `test_embedding_openrouter` cases covering the env-var fallback
  behavior and the custom-env opt-out).
- 46/46 e2e.
- post-install smoke clean (Gemini embed OK; OpenAI + S2 expected
  skip for missing key / rate limit).
- Fresh-venv pip-wheel install: `kb-importer import papers
  --fulltext-provider openrouter --help` lists the new choice;
  `kb-mcp` embedding picks up both env-var paths correctly.

## [1.1.0] — 2026-04-24

First post-1.0 minor release. Adds a new embedding provider
(OpenRouter) plus three 1.0.0-audit fixes caught by an independent
review pass.

### Added

- **OpenRouter embedding provider** for the RAG / vector-index
  pipeline. `embeddings.provider: openrouter` in `kb-mcp.yaml`
  routes requests to https://openrouter.ai/api/v1 (OpenAI-wire
  compatible) using `OPENROUTER_API_KEY`. Default model is
  `openai/text-embedding-3-small` (1536 dim — same output as
  direct OpenAI, useful when a user already has
  `OPENROUTER_API_KEY` but not `OPENAI_API_KEY`). The stored
  `papers.embedding_model` column carries an `openrouter/` prefix
  (e.g. `openrouter/openai/text-embedding-3-small`) so switching
  between direct OpenAI and OpenRouter-routed OpenAI triggers a
  re-embed rather than silently reusing cached vectors.

  The dim-lookup table in `kb_mcp.embedding._model_dim` now
  recognises vendor-prefixed names. Supported out of the box:
  `openai/text-embedding-3-*`, `voyage-ai/voyage-3*`,
  `bge-*-en-v1.5`. Models not in the table can still be used via
  an explicit `embeddings.dim: <N>` override in kb-mcp.yaml.

- **`scripts/pre_release_full_check.sh`** — six-step battery
  required before any public 1.x tag: lints, byte-compile, unit
  tests, e2e, post-install smoke, release-zip build. The
  lightweight `make_release.sh` still runs the four lints +
  builds the zip for dev iterations, but a Production/Stable
  release requires the full script to pass. Documented in
  CONTRIBUTING.md's Releasing section.

### Changed

- **Scoping note added to both kb-mcp.yaml scaffold and the
  kb-mcp README**: the `embeddings:` section configures the RAG
  pipeline only. Paper summaries written by `kb-importer
  --fulltext` use a completely separate LLM stack
  (`--fulltext-provider` / `--fulltext-model` / `kb_importer.summarize`).
  Changing one never alters the other. Root README grew a
  "Two LLM configurations, never mixed" table to make the
  separation obvious upfront.

- **PyPI classifier** on all five packages flipped
  `Development Status :: 4 - Beta` →
  `Development Status :: 5 - Production/Stable`. 1.0.0 claimed
  first stable but the classifiers still said Beta —
  inconsistent. Fixed across the bundle.

- **Root README section heading** `What's in this version
  (Phase 4 — v26)` renamed to `Architecture` + `**Data model.**`
  subheading. The internal phase / v26 naming is less useful
  once 1.x ships.

### Verification

- Lints clean (four checks + stdlib-usage sweep).
- Unit tests 415/415 (was 404 pre-1.1; +11 new `test_embedding_openrouter`
  cases cover: provider registration, default resolution, missing-key
  graceful degrade, vendor-prefix dim lookup, model-name prefix,
  explicit dim override, unknown model helpful error).
- E2E 46/46.
- `pre_release_full_check.sh` green end-to-end including release zip
  rebuild.

## [1.0.0] — 2026-04-24

**First stable release.** The 0.29.x series (0.29.0 → 0.29.8)
hardened the pip-wheel install path, the workspace autodetect,
error-path robustness, and doc accuracy. Audit sweeps in 0.29.3
through 0.29.8 caught and fixed a steady stream of small
silent-failure bugs; 1.0.0 is the point where the toolchain passes
every audit we currently have, on every install shape we currently
test.

No new features beyond 0.29.8. This bump exists purely to mark
"we're confident enough in the surface area and implementation
details to stop calling this a pre-release".

### What 1.0.0 promises

- **CLI surface stability within 1.x.** Subcommand names, flag
  names, and their argparse contracts won't change without a major
  bump. Adding new flags / subcommands is a minor bump; removing or
  renaming is major.
- **Config-file schema stability within 1.x.** Scaffold templates
  may add new optional keys in minor bumps. Renames carry a
  DeprecationWarning for at least one minor cycle.
- **Projection DB schema changes follow
  `kb_core.schema.SCHEMA_VERSION`.** A schema bump can happen in
  any minor release; the migration is always drop-and-rebuild on
  the next `ensure_schema()`. CHANGELOG will call it out
  explicitly.
- **Cross-package pin**: every release bumps all five packages
  together and pins `kb-*==<same-version>`. A `pip install -e` of
  one package against a different-version install of another
  refuses to resolve.

### Not promised

- Python API stability of internal modules (anything not in the
  package's `__init__.py` `__all__` list). They can reshuffle in
  any release.
- MCP tool argument schemas beyond what's stable enough to document
  in the exposed JSON schema. Adding optional args = minor; renaming
  an existing one = major.
- Performance numbers. "fast enough for 1200 papers on an SSD" is
  the ambient standard; we'll fix regressions but won't benchmark.

### Verification before release

- Four lints (`check_package_consistency`, `check_no_secrets`,
  `check_no_system_paths`, `check_cross_module_imports` with
  stdlib-usage sweep): clean.
- Unit tests: 404 / 404.
- E2E: 46 / 46 including MCP tool-count=36 assertion.
- post_install_test.py on fresh pip-wheel install: 14 pass, 1 skip
  (no OpenAI key), 1 unrelated external API flake.
- Fresh-venv acceptance test: scratch venv outside workspace +
  scratch workspace + `kb-write init` + `kb-importer status` +
  `kb-mcp index` + `kb-mcp snapshot export/import` round-trip +
  unicode body + mtime guard + malformed YAML + non-git kb_root
  + no-embedding-key graceful degradation — all behaved correctly.

## [0.29.8] — 2026-04-24

Fresh-venv acceptance test caught two silent-failure bugs: init
skipped config scaffolds when `.ee-kb-tools/` didn't exist, and
doctor silently passed md files with broken YAML.

### Fixed

- **`kb-write init` now auto-creates `.ee-kb-tools/config/` in the
  canonical workspace layout.** Pre-0.29.8, init only scaffolded
  config YAMLs if `<kb_root>/../.ee-kb-tools/` already existed —
  policy comment cited "don't create files outside kb_root
  unexpectedly". But a user following the fresh-workspace flow
  (`mkdir -p workspace/{ee-kb,zotero/storage} && cd ee-kb &&
  kb-write init`) ended up with a working KB scaffold and no
  config files, then hit unhelpful autodetect errors later.

  New rule: if `kb_root.name == "ee-kb"` (canonical layout
  indicated by directory name), init treats the parent as a
  workspace parent and creates `.ee-kb-tools/config/` with the
  four scaffolds. Non-canonical `kb_root` names still skip —
  avoids polluting `$HOME/` if a user points `--kb-root` at
  `$HOME/research/` rather than a dedicated `ee-kb` dir. The
  pre-existing "don't overwrite existing configs even with
  --force" guard is unchanged.

- **`kb-write doctor` now catches broken frontmatter.**
  Pre-0.29.8 doctor's per-type checks (papers / thoughts / topics)
  called `frontmatter.load()` with `except Exception: continue` —
  silently skipping unparseable files on the assumption another
  check would surface them. Not true for thoughts / topics: no
  other check parsed their YAML. Result: `doctor` reported "0
  findings" on a thought with unterminated brackets. Added a
  dedicated `_check_parse_errors` pass that runs first across all
  content subdirs (papers/, topics/standalone-note/,
  topics/agent-created/, thoughts/, .agent-prefs/) and flags each
  unparseable md as a `parse-error` finding with severity=error.

### Verification

- Reproduced both bugs on 0.29.7, then verified fixes in a fresh
  /tmp/acceptance venv + /tmp/ws_acc scratch workspace.
- All four lints + 404 unit tests green.
- Scaffold-presence consistency check still clean.

## [0.29.7] — 2026-04-24

Tightens argparse validators to close silent-corruption /
silent-rejection-of-documented-sentinel gaps spotted while auditing
every numeric flag across the four CLIs.

### Fixed

- **`kb-importer list --limit <neg>` no longer silently slices from
  the end.** `--limit` was `type=int, default=0` with help claiming
  "0 = no limit". Negative values took the Python slice-from-end
  path (`sorted_keys[:-5]` drops the last 5), producing quietly
  wrong output instead of an error. Switched to `_nonnegative_int`
  (same helper introduced for `kb-citations --freshness-days` in
  0.29.4), so `--limit -5` now errors with `must be >= 0, got -5`
  while `--limit 0` still means "no limit".

- **`kb-importer import --fulltext-max-tokens <= 0` now rejected.**
  The flag was `type=int`, so `--fulltext-max-tokens 0` was accepted
  — but a zero/negative token budget just produces truncated LLM
  responses and a confusing "non-JSON twice" error downstream.
  Switched to `_positive_int`.

### Added

- **`kb_importer/commands/_shared.py`** — local `_positive_int` /
  `_nonnegative_int` argparse validators, matching the pattern in
  `kb_write/commands/_shared.py`. Both imported by `list_cmd.py`
  and `import_cmd.py` so validator definitions don't drift.

### Verification

- All four lints + unit tests (404/404) green.
- Empirical: `kb-importer list papers --limit -5` prints
  `must be >= 0, got -5`; `--limit 0 --help` still documents
  "no limit"; `--fulltext-max-tokens 0` prints `must be positive,
  got 0`.

## [0.29.6] — 2026-04-24

Finishes the 0.29.5 workspace-autodetect tidy-up, plus adds a
user-facing upgrade guide that had been missing.

### Fixed

- **Unified CWD-first ordering across all three workspace
  autodetect sites.** 0.29.4 and 0.29.5 fixed individual sites
  one at a time; the pattern hadn't been applied uniformly.
  `kb_importer._find_workspace_config` and
  `kb_citations.find_workspace_config` / `kb_root_from_env`
  previously tried install-location first and CWD second. Under
  an editable install, this resolved to the *dev* workspace's
  config regardless of where the user had cd'd. Re-ordered all
  three to CWD-first, install-location as compatibility
  fallback — matches the pattern already in
  `kb_importer.config.load_config()` step 4b.

  Practical effect: a developer running editable-installed
  kb-tools from `~/dev/` can now `cd ~/work-project/ee-kb &&
  kb-importer status` and it resolves the work-project
  workspace, not their own dev one.

### Added

- **`UPGRADING.md`** — explicit version-bump procedure for
  existing workspaces. Covers: pattern A (`.ee-kb-tools/` as
  its own git clone) and pattern B (deploy.sh re-run) upgrade
  paths, pre-upgrade snapshot procedure, schema-bump handling
  (auto-drop + rebuild on ensure_schema mismatch), config
  migrations (DeprecationWarning for renamed keys, additive
  rescaffolds), validation checklist, rollback flow (including
  `snapshot import --force` for schema downgrade), and the
  common upgrade-time errors mapped back to their CHANGELOG
  entries. Linked from README's introduction alongside
  DEPLOYMENT.md.

### Verification

- All four lints clean.
- 404/404 unit tests pass.
- Editable-install empirical test: `cd /tmp/ws/ee-kb` (workspace
  A) with editable install from a separate workspace B now resolves
  `_find_workspace_config` / `find_workspace_config` /
  `kb_root_from_env` all to workspace A.

## [0.29.5] — 2026-04-24

Finishes the 0.29.4 workspace-autodetect fix. A second audit caught
that 0.29.4 only fixed the config-file lookup, not the `kb_root` /
`zotero_storage_dir` derivation — so a pip-wheel user who cd'd into
their workspace got "zotero_storage_dir is required" despite a
correctly-placed scaffolded config.

### Fixed

- **`kb_importer.config.load_config()` step 4b now autodetects
  kb_root and zotero_storage_dir via CWD first.** Previously this
  block walked up from `Path(__file__).resolve()` only — the install
  location. That works for `scripts/deploy.sh` layouts (venv inside
  `.ee-kb-tools/`) but silently misses for pip-wheel installs where
  the venv lives elsewhere. Worse, in the editable-install case it
  resolved to the *dev* workspace instead of the user's actual
  workspace, because the CWD was never consulted.

  Now runs two passes: (a) `find_workspace_root()` from CWD, (b)
  the old install-location walk as compatibility fallback. CWD
  takes precedence — the user's current directory authoritatively
  names the workspace. Verified end-to-end: fresh venv under
  `/tmp/kbtest/` with wheels installed + a workspace at
  `/tmp/ws/` + `cd /tmp/ws/ee-kb && kb-importer status` now
  resolves `kb_root=/tmp/ws/ee-kb` and
  `zotero_storage=/tmp/ws/zotero/storage` without any env vars
  or CLI flags.

  The 0.29.4 fix to `_find_workspace_config` (config file lookup)
  was correct and is retained — this patch covers the second
  half of the same pattern that was missed.

### Changed

- **README "Bootstrapping a new KB" section** updated: install
  block now lists all five packages (was missing `kb_core` and
  `kb_citations`), with `kb_core` first so the others resolve
  their `kb-core==` dep from the local checkout. The "copy the
  three package directories in" comment was stale since the
  kb_core extraction and the 0.29.0 kb_citations split; now
  reads "five package directories".

### Verification

- Unit tests: all passing.
- Cross-module lint + package consistency + no-secrets +
  no-system-paths: clean.
- Reproduced the failure on an unpatched 0.29.4 wheel (cwd-in-
  workspace + venv-outside-workspace → "zotero_storage_dir is
  required") then confirmed the patched 0.29.5 wheel resolves
  both paths correctly in the same setup.

## [0.29.4] — 2026-04-23

Pre-release audit of the pushed 0.29.3 caught six real issues —
mostly paper cuts, one runtime blocker. Fixing them required
extending the cross-module lint to also catch stdlib-module-used-
but-not-imported, which immediately found two more copies of the
same bug the reviewer had spotted.

### Fixed

- **`import sys` missing in three `kb_importer.commands` modules.**
  The 0.28.0 G-split moved error-path `sys.stderr` writes from
  `import_cmd.py` into `import_fulltext.py`, `import_pipeline.py`,
  and `import_keys.py` without carrying the `import sys` line.
  Every error path in those files raised `NameError: sys` instead
  of printing the intended warning. Added `import sys` to all
  three. The reviewer flagged `import_fulltext.py`; the extended
  lint (below) caught the other two.

- **Config autodetect now works under pip-wheel installs, not just
  `scripts/deploy.sh` layouts.** `kb_importer._find_workspace_config`,
  `kb_citations.config.find_workspace_config`, and
  `kb_citations.config.kb_root_from_env` previously called only
  `kb_core.workspace.find_tools_dir()` — which walks up from the
  installed module's location. A pip-installed wheel lives in
  `site-packages/`, far from any user's workspace, so that walk
  always terminated outside `.ee-kb-tools/` and autodetect returned
  None. Added a second step: if `find_tools_dir()` misses, fall
  back to `find_workspace_root()` which walks up from CWD. Now
  `cd .ee-kb-tools && kb-importer status` (or `kb-citations
  status`) resolves its config from the non-editable install.

- **`kb-citations --freshness-days 0` no longer rejected by argparse.**
  Help text documented 0 as "force refetch" (and the downstream
  `_build_ctx` maps 0 → None correctly), but the argparse validator
  was `_positive_int`, which rejected any non-positive value. Added
  `_nonnegative_int` and wired it to `--freshness-days` only. Other
  numeric flags (`--max-refs`, `--max-cites`, `--max-api-calls`,
  `--min-cites`, `--limit`) still use `_positive_int` — zero is
  meaningless for them.

- **`kb-importer --zotero-source` help text claimed `live` was the
  default**, three releases after 0.28.0 flipped the default to
  `web`. Rewrote both the subcommand help and the top-level
  description so they match the code.

### Added

- **`check_cross_module_imports.py` extended with
  `check_stdlib_usage()`**. Previously it only caught "symbol used
  in one submodule but defined in another" across the v0.28 G-split
  groups. Now it also catches "stdlib module attribute used but the
  module never imported" (e.g. `sys.stderr` used with no
  `import sys`). STDLIB_ROOTS is an explicit allow-list
  (`sys`, `os`, `re`, `json`, `pathlib`, `subprocess`, `threading`,
  `time`, `datetime`, `logging`, `shutil`, `argparse`, `sqlite3`,
  `struct`) — adding to it is a deliberate act. Running the lint
  caught two extra copies of the sys-import bug beyond the one the
  reviewer found.

### Changed

- **Docs sweep for package count and removed flags.** `README.md`,
  `kb_importer/README.md`, and `CONTRIBUTING.md` had multiple stale
  references from before the kb_core extraction and the 0.29.1
  `_archived/` removal: several "4 packages" / "four packages"
  lines, install blocks that missed `kb_core` (which other packages
  now pin as a versioned dep), a stray `unarchive` subcommand
  reference, and a `kb-citations fetch --only-key` / `refresh-counts
  --only-key` claim (neither subcommand has that flag). All
  corrected. Install order now documents `kb_core` first — without
  it on the path, the other four editable installs can't resolve
  their `kb-core==` pin.

### Verification

- `scripts/check_cross_module_imports.py` (with the new stdlib
  sweep) clean across all five packages.
- `scripts/check_package_consistency.py` clean — version bump
  propagated across 10 `__version__` / `version =` sites and 7
  inter-package pins.
- Fresh `python -m build --wheel` + `pip install` into a scratch
  venv: `kb-importer --help`, `kb-citations fetch --help`, and
  `kb-citations fetch --freshness-days 0` all parse without error.

## [0.29.3] — 2026-04

Repairs 0.29.2 — which claimed to fix scaffold-template packaging
but actually landed with the three templates never committed to
git, and separately ships a runtime NameError fix from the 0.28
kb-importer split.

### Fixed

- **Scaffold config yamls now actually in the repo.**
  0.29.2 added `force-include` clauses in `kb_write/pyproject.toml`
  pointing at
    kb_write/src/kb_write/scaffold/config_kb_importer.yaml
    kb_write/src/kb_write/scaffold/config_kb_mcp.yaml
    kb_write/src/kb_write/scaffold/config_kb_citations.yaml
  but those files were never committed — `.gitignore` has bare-name
  rules `config_kb_*.yaml` (intended to catch user real-config
  copies leaking into the repo) that also shadowed the scaffold
  templates. The files existed on my local disk, so Hatch's
  force-include succeeded at build time and the wheel I tested
  did have them; but anyone cloning from GitHub got a working
  tree without the files. A wheel built from such a clone would
  either fail or silently produce an empty scaffold.

  Root fix: `git add -f` the three yamls. They're now properly
  tracked. A new consistency check
  (`check_scaffold_templates_present` in
  `scripts/check_package_consistency.py`) asserts, at release
  time, that the four scaffold files exist on disk AND appear in
  `git ls-files`. Running the consistency script would have
  caught 0.29.2 pre-push.

- **`_auto_commit_single_paper` missing import in
  `import_fulltext.py`.** The 0.28.0 G-split moved this helper
  from the monolithic `import_cmd.py` into `import_pipeline.py`
  but this file's two call sites (lines 567 and 800) never got
  an `import` added for it. The fulltext pipeline's per-paper
  git commit path therefore raised `NameError:
  _auto_commit_single_paper` at runtime. Unit tests didn't cover
  that path. Fixed by adding
  `from .import_pipeline import _auto_commit_single_paper`.

- **`kb-write init` no longer silently swallows missing-scaffold
  errors.** Pre-0.29.3, a missing scaffold template (e.g. the
  0.29.1 / 0.29.2 packaging regression) triggered a defensive
  `try/except FileNotFoundError: continue`, so operators saw no
  config get created AND no error. Now raises `RuntimeError`
  with a pointed "packaging error: re-install from a correctly
  built wheel" message. Second line of defence behind the new
  consistency check.

### Added

- **`scripts/check_cross_module_imports.py`** — AST-scans the four
  v0.28 split groups (kb_importer/commands/, kb_write/commands/,
  kb_mcp/(indexer submodules), kb_mcp/(server_cli)) and flags any
  function/class used in one sibling but defined in another
  without an import line. Catches the class of bug that produced
  the `_auto_commit_single_paper` NameError — a symbol present in
  `defined_by`, referenced but not imported, and not locally
  bound. Currently clean across the whole repo.

- **`check_package_consistency.py` gains
  `check_scaffold_templates_present`** — asserts the four scaffold
  files exist on disk AND are in `git ls-files`. The "tracked"
  half specifically defends against `.gitignore`-shadowed files
  that appear to work for editable installs and local builds but
  break fresh clones.

### Changed

- **`config_kb_importer.yaml` scaffold** flipped `source_mode:
  live` → `source_mode: web` to match the 0.28.0 code default. The
  old value would have silently given new users the `live` (needs
  Zotero desktop) path instead of the now-default `web` path.
- **`config_README.md`** adds the missing `kb-citations.yaml`
  row to the file table.

### Verification (what I actually ran this time)

- `python -m build --wheel --sdist` on a fresh venv (not editable)
  → wheel contains all three scaffold yamls: `config_kb_*.yaml`
- `pip install kb_core-*.whl kb_write-*.whl` into another fresh
  venv → `kb-write init` in a workspace writes
  `.ee-kb-tools/config/{kb-mcp,kb-importer,kb-citations}.yaml +
  README.md` — four files, all four created.
- Both new lint scripts (cross-module + scaffold-presence) pass
  on current tree and fire on the un-tracked case.

## [0.29.2] — 2026-04

Packaging bug fix. 0.29.1 shipped a wheel that was missing three
scaffold templates — `kb-write init` silently skipped creating
`<workspace>/.ee-kb-tools/config/{kb-importer,kb-mcp,kb-citations}.yaml`
because the files weren't in the installed package. This was the
root cause behind "0.29.1 不再随包附带 config/ 目录，但代码仍从
.ee-kb-tools/config/kb-importer.yaml 读取配置" user observation.

### Fixed

- `kb_write/pyproject.toml` now force-includes the three scaffold
  yaml templates in both wheel and sdist targets.

Root cause: the repo's `.gitignore` has a rule

    config_kb_importer.yaml
    config_kb_mcp.yaml
    config_kb_citations.yaml

to catch user-real-config copies leaking into the repo. Hatch
(the build backend) honours gitignore patterns by default when
assembling the wheel, so it was dropping the scaffold templates
at `kb_write/src/kb_write/scaffold/config_kb_*.yaml` — same
filename as the gitignore rule. The wheel therefore landed
without them, `kb-write init`'s `importlib.resources.read_text`
raised FileNotFoundError, which init caught via a defensive
try/except and silently skipped. Operators saw: no config/, no
error, no clue.

The release zip built by `scripts/make_release.sh` was
unaffected — it uses `python3 -m zipfile` or `zip -r`, neither
of which honours gitignore. Only the pip-installable wheel was
missing the files. Consequently the issue was invisible in our
stress-run's post-install test against the editable install but
showed up in the user's real deployment.

### Added

- End-to-end verification in the fresh-venv test path confirms
  `pip install` → `kb-write init` does populate
  `<workspace>/.ee-kb-tools/config/` with the three yaml files
  plus `README.md`.

## [0.29.1] — 2026-04

Completes the `_archived/` removal started in 0.29.0. 0.29.0 turned
off the auto-archive step but kept compatibility shims; 0.29.1
deletes the whole feature. **No back-compat** for KBs that still
have PDFs under a legacy `storage/_archived/` — they must be
flattened manually before upgrading:

    mv storage/_archived/*/ storage/ && rmdir storage/_archived

### Removed

- `ARCHIVE_SUBDIR` constant
- `ArchiveResult` dataclass
- `archive_attachments()` function (was no-op + DeprecationWarning)
- `unarchive_attachments()` function
- `Config.archive_dir` property
- `AttachmentScan.archived` + `AttachmentScan.unarchived` fields
  (collapsed into a single `AttachmentScan.dirs` set)
- `kb-importer unarchive` CLI subcommand
- `is_archived` flag from `find_pdf()` return (now `Path | None`
  instead of `tuple[Path | None, bool]`)
- `is_archived` from `attachment_locations` tuples in
  `_build_paper_body` and `build_paper_md`
- `_archived/` fallback in `kb_write.ops.re_read_sources.papers_with_pdf`
- `unreferenced_archived` / `unreferenced_unarchived` split in
  `detect_orphans()` — now a single `unreferenced_dirs` list
- `kb-mcp report`'s archived-bucket section

### Changed

- `scan_attachments()` only walks `storage/` (no more `_archived/`
  traversal).
- `status_cmd` shows `Attachment storage dirs: Total: N`; the
  archived/unarchived split is gone.
- `orphans_cmd` no longer mentions archive paths.
- Importer/README updated.

### Net effect

Zero code paths in 0.29.1 read from, write to, or create a
`_archived/` directory. The failure mode the whole redesign targeted
(Zotero API blip → attachment dir shuffled → max_child_version
reset → md mtime churn → kb-mcp reindex storm) cannot recur because
the machinery that moved files is gone.

## [0.29.0] — 2026-04

Focused release: removes a bug-causing feature (auto-archive) and
fixes the root cause of attachment-state thrashing.

### Fixed

- **`_fetch_children` no longer silently swallows API errors.**
  Pre-0.29, `zotero_reader._fetch_children` caught every exception
  from `self._z.children()` and returned `([], [])`. Any transient
  Zotero API failure (network blip, rate limit, auth renewal,
  server 5xx) made papers with real PDFs appear attachment-less.
  The import pipeline then rewrote the paper md with
  `zotero_attachment_keys: []` and `zotero_max_child_version: 0`;
  on the next successful fetch the values swung back. Visible
  symptoms: papers oscillating between "has-PDF" / "no-PDF" in
  the KB, `storage/_archived/` getting shuffled repeatedly,
  per-paper md mtimes advancing on every sync, kb-mcp reindexing
  everything, `zotero_max_child_version` jumping around.

  Fix: new typed `ZoteroChildrenFetchError` propagates up. The
  top-level import loop catches this specific error and SKIPS the
  paper's md rewrite — transient blips leave the paper unchanged.
  Operator sees `⚠ KEY SKIPPED: could not fetch children from
  Zotero (...). The md was left UNCHANGED. Re-run when the
  Zotero API is healthy.` instead of silent corruption.

### Removed

- **Auto-archive of attachment directories after import.**
  Pre-0.29, each successful paper import moved
  `storage/<attachment_key>/` → `storage/_archived/<attachment_key>/`
  "to keep `ls storage/` uncluttered". In combination with the
  `_fetch_children` swallow bug (above), this caused attachment
  dirs to be moved on every blip-then-recover cycle. The feature
  provided no functional benefit — attachments are keyed by Zotero
  key; `find_pdf()` already resolves both locations transparently.

  0.29 makes `archive_attachments()` a no-op with
  `DeprecationWarning`. The auto-archive call site in
  `import_pipeline._process_paper` is gone. PDFs stay in
  `storage/` permanently after import.

  **Back-compat:** installations upgrading from <0.29 may have
  PDFs under `storage/_archived/` from past runs. `find_pdf()`
  still resolves that path, so nothing breaks. For disk hygiene,
  operators can use `kb-importer orphans --unarchive` (which
  remains functional) to pull files back into `storage/`, or
  delete `storage/_archived/` contents after verifying they're
  not needed.

### Documentation

- **`sync_cmd.py` module docstring adds a Q&A section on
  resetting `zotero_*` version fields to 0.** Short answer: safe
  — forces a one-time re-import sweep; idempotent and
  preserves the AI zone. Includes a Python one-liner for bulk
  reset, useful for recovering from the pre-0.29 thrash-induced
  md corruption.

### Migration note for operators upgrading from <0.29

If your library was hit by the pre-0.29 thrash (papers showing
attachment drops after transient API errors), the recommended
recovery is:

1. Upgrade to 0.29 (fixes root cause).
2. Optionally: run the reset-to-0 snippet from `sync_cmd.py`'s
   docstring on your papers/ dir.
3. Run `kb-importer sync papers` — all papers re-import from
   current Zotero state, attachment_keys + child_version fields
   become correct, md mtimes advance ONCE and then stabilise.
4. Optionally: inspect `storage/_archived/` and decide whether
   to `kb-importer orphans --unarchive` back to `storage/` or
   just delete it.

No data loss; AI zones are preserved across re-imports via the
existing `extract_preserved` mechanism.

## [0.28.2] — 2026-04

Bug-fix release from two independent code reviews plus the
internal stress-run findings. Nine fixes and one lint-hardening.

### Fixed

- **kb_write RMW no longer silently corrupts BOM-prefixed md.**
  Pre-0.28.2, `kb-write tag add` / `ref add` / `ai-zone append` on a
  file that happened to have a UTF-8 BOM at byte 0 would rewrite it
  with `kind: null`, `title: null`, BOM and CRLF both gone — because
  python-frontmatter doesn't recognise `<BOM>---` as a delimiter.
  `kb-mcp index` then correctly skipped the paper, leaving it
  orphaned. Now `kb_write.frontmatter.read_md` refuses BOM-prefixed
  files up-front with a pointed error, and also refuses any md whose
  frontmatter parses to missing-or-None `kind`. Stress-run finding
  G54; tests at `tests/unit/test_read_md_bom_guard.py`.

- **Gemini provider raises `BadRequestError` typed, not generic
  `SummarizerError`, for HTTP 400 / 404.** The class already existed
  (code='bad_request'); the provider just wasn't using it. Reviewer:
  "分类但不调度" — categorisation existed but didn't change retry
  behavior. Fixed: (a) 400/404 now raise `BadRequestError`, (b) the
  short + longform retry loops in `import_fulltext` have a new
  except-branch that, on "model not found / invalid" shapes,
  activates the fulltext fallback model (same path as quota). For
  per-paper BadRequest (e.g. fulltext length overflow), the paper
  is recorded as failed and the batch continues. Event
  classification also drops the `"400" in err_text` string match in
  favour of `isinstance(last_err, BadRequestError)`.
  Tests at `tests/unit/test_bad_request_classification.py`.

- **`kb_core.paths.safe_resolve` rejects whitespace-only paths.**
  The function's docstring promised rejection but the implementation
  only checked `if not rel`. A literal `"   "` would slip through to
  be resolved as a whitespace filename. Now `if not rel or not
  rel.strip()`. Test case added.

- **`validate_kb_ref_entry` tightened with per-type tail validation.**
  Pre-0.28.2 these shapes all leaked through: `papers/`,
  `thoughts/`, `topics/agent-created/`, `topics/agent-created/a/b/c`,
  `topics/standalone-note/`, `papers/BAD SLUG`, `papers/lowercase`,
  `thoughts/not-date-prefixed`. Each prefix now enforces exactly one
  tail segment AND validates it: papers needs an 8-char Zotero key
  (optionally `-chNN`), thoughts needs YYYY-MM-DD-kebab,
  agent-created topics need lowercase kebab, standalone-note needs
  a Zotero key. New test file covers 38 cases.

- **kb-mcp index promotes dangling edges when the target lands.**
  Pre-0.28.2, `link_resolve.resolve_staged_links` only re-staged
  edges for SRC mds whose mtime advanced. A user who imported paper
  B after A pointed to it via `kb_refs: [papers/B]` had to ALSO
  touch A for the edge to promote from dangling→paper — despite the
  docstring claiming automatic promotion. Now a dedicated
  `_promote_dangling_edges` pass runs at the end of every
  `resolve_staged_links` and rewrites any `dst_type='dangling'`
  rows whose key now resolves in the node tables. Cost: O(dangling
  rows) — typically 0-few. IndexReport now carries a
  `links_promoted` count. Stress-run finding G18; tests at
  `tests/unit/test_dangling_promotion.py`.

- **kb-write `--dry-run` preview on would-change ops.** Pre-0.28.2,
  a dry-run tag/ref/ai-zone that WOULD make a change printed
  "(no changes — write would be a no-op)" because the op returned
  `WriteResult` with empty `diff` / `preview`. `_emit_result` then
  fell through to the no-changes branch. Now each op populates
  `preview` with a before/after rendering for both would-change and
  would-be-no-op cases. Stress-run finding G34.

- **kb-citations link on missing DB with empty cache returns rc=0.**
  Pre-0.28.2, running `kb-citations link` on a KB without any cached
  citations AND without a kb-mcp DB printed both "falling back to
  JSONL dump" (log) AND "✗ link failed: DB error" (CLI summary) —
  contradictory messages, rc=1. Since there are no edges to write,
  the DB unavailability is inconsequential. Now reported as
  `i no edges to write ... kb-mcp DB unavailable but not needed`
  with rc=0. Stress-run finding G96.

- **`make_thought_slug` appends entropy when title is all
  non-ASCII.** Pre-0.28.2, a Chinese / emoji / punctuation-only
  title would ASCII-strip to nothing and fall back to the literal
  word `thought`, making every same-day non-ASCII thought slug
  collide on `YYYY-MM-DD-thought`. Second creation fell through to
  WriteExistsError. Now we suffix 6 hex chars of entropy when the
  fallback word is used, so each auto-slug stays unique. Operators
  are still encouraged to pass an explicit `--slug` or English
  title. Stress-run finding G10.

### Documented

- **Schema history entry for v7** in `kb_mcp/store.py`. v7 went
  live in 0.27.1 (repointed four side-table FKs from
  `papers(zotero_key)` to `papers(paper_key)` after v6 made the
  former non-unique) but the history comment block only documented
  up to v6 — the same comment even warned "A missing entry is a
  lint failure waiting to happen". Added the missing entry.

### Added

- **`scripts/check_package_consistency.py` now verifies schema
  history completeness.** New `check_schema_history_complete()`
  function parses `EXPECTED_SCHEMA_VERSION = N` and asserts the
  history comment block contains one `# vX = ...` line for each X
  in `1..N`. Prevents the class of bug that let v7 ship
  undocumented.

## [0.28.1] — 2026-04

Bug fix + doc-correction release from external reviewer feedback
on the 0.28.0 bundle.

### Fixed

- **Gemini 2.5-pro fallback path was sending `thinkingBudget: 0`,
  which that model rejects with HTTP 400 "Budget 0 is invalid.
  This model only works in thinking mode."** The pre-0.28.1 branch
  `elif m.startswith("gemini-2.5"): thinkingBudget = 0` was
  correct for `gemini-2.5-flash` and `gemini-2.5-flash-lite` (both
  accept 0 to disable thinking) but silently wrong for
  `gemini-2.5-pro` (which always thinks and requires the budget
  in [128, 32768] or -1 for dynamic).

  Practical impact: `--fulltext-fallback-model` defaults to
  `gemini-2.5-pro` because it has a much larger RPD allowance
  than the primary `gemini-3.1-pro-preview` (250/day). When the
  primary's quota ran out mid-batch and the pipeline switched to
  the fallback, every remaining paper hit the 400 and was counted
  as llm-fail — exactly the hundreds-of-papers-stuck state users
  saw after the 3.1-pro-preview daily quota was exhausted.

  Fix: the `gemini-2.5-*` branch now splits on variant. flash
  variants still send `thinkingBudget: 0`; pro (and any unknown
  2.5-* future variant) gets `thinkingBudget: 128` — the
  documented minimum, keeping thinking-token cost minimal while
  respecting the API's contract. Locked by
  `tests/unit/test_gemini_thinking_config.py` (5 cases).

### Fixed (docs)

Reviewer audit of the 0.28.0 bundle flagged four places where
documentation described the v25 or pre-refactor API instead of
what the CLI actually accepts. All four would have made agents /
users fail on first invocation:

- `kb_write/README.md` said `kb-write ai-zone update ...`, but v26
  replaced `update` with append-only `ai-zone append`. Corrected
  to `ai-zone append KEY --expected-mtime ... --title "..." --body-file ... [--date YYYY-MM-DD]`.
- `kb_write/README.md` Python API example called
  `ai_zone.append(..., body_md=..., date="2026-04-22")`, but the
  real signature is `append(ctx, target, expected_mtime, *, title,
  body, entry_date)`. Example corrected: `expected_mtime` is
  required, body keyword is `body` (not `body_md`), date keyword is
  `entry_date` and takes a `datetime.date` (not a string).
- `kb_write/src/kb_write/prompts/fragments/write_workflow.md`
  (shown to agents as on-disk guidance) also carried the stale
  `ai-zone update` form AND used `kb-write ref add ... --target
  papers/KEY` (correct flag is `--target-ref` / `--ref`). Both
  corrected.
- Root `README.md` stated "`kb_citations` hard-depends on
  `kb_mcp`". In reality the dependency is split: `kb-citations
  fetch` runs standalone (writes a JSONL cache that can be used
  as fallback), while `link` and `refresh-counts` need kb_mcp to
  write into the projection DB. `pyproject.toml` pins `kb-mcp`
  under the `link` extra, not as a hard dep. Description updated.

### Added

- **`scripts/make_release.sh`** — build a clean release zip. The
  0.28.0 release zip shipped with 187 `__pycache__/*.pyc` entries
  (bytecode from the packager's Python version), which bloats the
  artefact and can cause surprising import behaviour if the
  receiver's Python differs. The new script stages a sanitised
  copy with explicit exclusions, runs the consistency /
  no-secrets / no-system-paths gates as pre-flight, zips, and
  asserts 0 bytecode entries in the final zip before reporting
  success. Works without rsync or zip (falls back to cp -a +
  python3 -m zipfile from stdlib).

## [0.28.0] — 2026-04

A feature + hardening release: new migration & doctor-fix surface,
the end of the lock-re-entrancy / partial-embed saga, and a
substantial code-organisation pass that splits four multi-thousand-
line modules into focused submodules. Zotero web becomes the
default metadata source.

### Added

- **`kb-write migrate-slugs`** — one-shot rename of thought mds
  whose slugs violate the canonical lowercase-kebab format
  (`^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9\-]*$`). Pre-v24 imports
  wrote uppercase Zotero keys directly into thought filenames
  (e.g. `2026-04-22-ABCD1234-note.md`); v24's slug rule flagged
  these as violations but offered no migrator. The new tool
  canonicalises in place — preserves the YYYY-MM-DD date,
  lowercases the rest, substitutes disallowed chars with `-`,
  collapses double-hyphens, strips edges. Refuses non-date-
  prefixed slugs (reported as errors) and collisions (reported,
  never overwrites). Idempotent — already-canonical files skipped.
  Atomic rename via `shutil.move`, one batch git commit per run,
  audit.log entries. `--dry-run` for preview.

- **`kb-write doctor --fix` now dedupes kb_refs / kb_tags /
  authors.** New check I flags list-field duplicates as a warning
  with auto_fixable=True. `--fix` rewrites the frontmatter with
  first-occurrence-wins order. Safe to auto-dedupe because these
  fields are set-semantics downstream; duplicates only clutter
  diffs. Malformed lists (non-list, non-string entries) are
  skipped so `_check_frontmatter_types` handles them without
  double-reporting.

- **Per-paper write lock.** `kb_write.atomic.write_lock_paper(
  kb_root, paper_key)` anchors at `.kb-mcp/paper-locks/<key>.lock`.
  Used by tag add/remove, ref add/remove, ai-zone append — all
  single-paper RMW ops. Different papers' writers now run in
  parallel; same-paper writers still serialise so the RMW race
  remains covered. Sanitises paper_key before filename use (the
  kb-mcp upstream validator already rejects weird keys but this is
  defense-in-depth).

### Changed

- **kb-importer default metadata source flipped from `live` to
  `web`.** Zotero web API (api.zotero.org) needs only an API key
  + library_id + network — no local Zotero install. Works
  uniformly from laptops and headless servers, which is why it's
  the portable default. `live` mode (localhost:23119, needs
  Zotero desktop running) still works; set `source_mode: live`
  in config or `--zotero-source live` on the CLI to get it.

- **`write_lock()` switched from O_EXCL + PID-file to
  `fcntl.flock`.** The prior protocol had an empty-file window
  at acquisition: between `O_CREAT|O_EXCL` and the PID write,
  another acquirer could unlink the empty file and recreate it,
  leading to two processes both believing they held the lock.
  The 0.27.4 field report of "95/100 tags landed out of 100
  concurrent writes" was diagnosed as this — not a lock
  re-entrancy bug as initially suspected. fcntl.flock uses
  kernel state, not file presence, so there's no empty window.
  Windows falls back to the PID-file shape (fcntl unavailable).

- **Marker constants consolidated.** `FULLTEXT_START` /
  `FULLTEXT_END` / `AI_ZONE_START` / `AI_ZONE_END` are now
  imported from their canonical kb_core / kb_write.zones
  sources instead of being re-declared across 8 sites. The
  `check_package_consistency` lint was extended to accept the
  `from kb_core import ..., NAME` form as well as the literal
  `NAME = "..."` form so the consolidation doesn't fail the check.

### Refactored (no behaviour change)

- **`kb_write/cli.py`: 1115 → 139 lines.** Split into `commands/`
  package with per-subcommand modules: init, node (thought+topic),
  pref, zone (ai-zone), field (tag+ref), admin (delete+log+rules+
  doctor), batch (re-summarize+re-read), migrate. Shared helpers
  (_resolve_kb_root, _build_context, _read_body, _emit_result)
  live in `commands/_shared.py`.

- **`kb_mcp/indexer.py`: 1449 → 745 lines.** Three self-contained
  passes extracted into sibling modules:
  - `embedding_pass.py` — run_embedding_pass + chunk_paper (Phase 2b).
  - `stale_cleanup.py` — remove_orphans + delete_stale_node_row.
  - `link_resolve.py` — stage_refs + resolve_staged_links.
  Module-level helpers (_extract_fulltext_body, _vec_blob, etc.)
  moved to `_indexer_helpers.py`; indexer.py re-exports them so
  `from kb_mcp.indexer import _extract_fulltext_body` keeps working.

- **`kb_importer/commands/import_cmd.py`: 1505 → 407 lines.** Split
  into three sibling modules: `import_keys.py` (key resolution),
  `import_pipeline.py` (per-item processing + auto-commit),
  `import_fulltext.py` (the 720-line PDF→LLM→writeback pass). The
  slimmed `import_cmd.py` re-imports all the moved symbols so
  `from kb_importer.commands.import_cmd import _process_paper`
  still works.

- **`kb_mcp/server.py`: 2132 → 1799 lines.** Extracted the argparse
  builder (165 lines), logging setup, and the three `_cmd_*_impl`
  citation subcommand implementations into `server_cli.py`. The
  @mcp.tool() registrations stay in server.py (they share the
  FastMCP instance). Impls now take `kb_root` as an explicit
  parameter rather than reaching into server's module state; the
  wrappers pass `_kb_root()` in.

## [0.27.10] — 2026-04

Five bug fixes from a fresh external review. One data-
consistency bug (#2) with silent downstream RAG degradation;
one latent re-entrancy bug (#1) that no current caller
triggers but any refactor could; one index-drift bug (#3);
two observability/reporting bugs (#4, #5). No behaviour
change on happy paths; all five are defensive improvements.

### Fixed

- **#2 Embedding pass mis-flagged partially-embedded papers
  as `embedded = 1`.** When a paper's chunks spanned multiple
  embedding batches and some non-first batch failed, the
  pre-0.27.10 code still added the paper to `success_papers`
  via the chunks from earlier successful batches. The
  post-loop `UPDATE papers SET embedded = 1 WHERE paper_key IN
  success_papers` then marked the paper fully embedded even
  though only some chunks had landed. The paper's md_mtime
  matched the row so future reindexes wouldn't re-queue it —
  RAG coverage silently degraded on the next API-quota hiccup
  and never self-healed.

  Fix: `_run_embedding_pass` now tracks per-paper
  `expected_per_paper` (chunks scheduled) and
  `inserted_per_paper` (chunks actually landed). After all
  batches it splits pending into `fully_embedded` (inserted
  == expected → `UPDATE embedded = 1`) and `partially_embedded`
  (inserted < expected → DELETE their partial chunk_meta +
  chunks_vec rows, leave `embedded = 0` so the next reindex
  retries cleanly). `report.embed_failed` counts by paper, not
  by batch, so the number reflects actual retry candidates.

  Locked by `tests/unit/test_embedding_partial_batch.py` —
  three cases: partial batch → embedded=0 and scrubbed,
  all-in-one-batch happy path, multi-batch all-succeed happy
  path.

- **#1 `write_lock()` deleted the on-disk lock on nested
  in-process acquisition.** When the same process called
  `write_lock(kb)` from within an already-held lock, the
  O_EXCL create would fail (file exists), the code read the
  PID from the lock file, saw it matched the current
  process, and fell through to the "Stale lock — take it
  over" branch. That path unlinked the lock file and
  recreated it. When the inner scope exited it unlinked the
  lock a second time — while the outer scope was still in
  its critical section — letting a sibling process walk in.

  No active caller in the current codebase re-enters
  `write_lock` (the one case that nearly does, `re_read →
  re_summarize`, was deliberately designed so `re_read`
  doesn't hold a lock). But the bug was one refactor away
  from firing, and the cost of fixing it now is small.

  Fix: added a module-level `_held_locks: dict[str, int]`
  tracking per-lock-path re-entry depth. On entry, if we
  already hold this path, bump the depth and yield without
  touching the file. On exit, decrement; only the outermost
  exit unlinks. The on-disk lock file remains the
  cross-process signal; the depth counter handles within-
  process nesting.

  Locked by 5 cases in `tests/unit/test_write_lock_reentry.py`:
  depth-2 nesting doesn't let inner exit kill outer, depth-3
  nesting works, sequential acquires still work, separate
  kb_roots don't share state, inner exception still decrements
  depth via finally.

- **#3 Indexer left stale DB rows when frontmatter became
  invalid.** If a previously-indexed paper had its `kind`
  field changed (e.g. user edited YAML, or a tool rewrote it)
  or its frontmatter was corrupted so `frontmatter.load()`
  raised, the indexer's kind-check branch just logged and
  returned (and the outer try/except just recorded the
  error). The DB row — plus paper_fts + paper_chunk_meta +
  paper_chunks_vec + outbound links — stayed. Search,
  backlinks, and graph queries kept returning a phantom node
  until the md file itself was deleted.

  Fix: new `_delete_stale_node_row(table, key)` helper that
  removes the main row + side tables + FTS + chunk data,
  reclassifies inbound links as 'dangling'. Wired into both
  (a) the kind-mismatch branch of `_index_paper` /
  `_index_note` and (b) the outer `except Exception` of all
  four `_index_*` methods. When a file fails to be a valid
  node, any row it had before is scrubbed.

  Locked by 5 cases in `tests/unit/test_indexer_stale_row_cleanup.py`:
  kind-mismatch removes row + side tables, parse-failure
  triggers cleanup from outer except, fresh file with
  bad kind is a clean no-op, helper is idempotent on
  missing keys, paper side tables are all scrubbed.

- **#4 `refresh-counts` `skipped_no_doi` over-counted by the
  chapter-row count.** `citation_ops.count_papers()` was a
  naked `SELECT COUNT(*) FROM papers`, but
  `list_papers_with_doi()` filtered to whole-work rows only
  (`paper_key = zotero_key`). In a library with book-chapter
  splits (one `BOOKKEY.md` row plus N `BOOKKEY-chNN.md`
  rows) `total_papers - len(papers_with_doi)` over-counted
  the "skipped" bucket by the chapter count. The report line
  `"N papers total, M with DOI, K skipped_no_doi"` misled
  users trying to decide whether `refresh-counts` had more
  work to do.

  Fix: `count_papers()` now filters the same way
  (`WHERE paper_key = zotero_key`). Not data corruption —
  the links table itself was always correct. Only the
  progress line and the final report struct had the
  mis-count.

  Locked by 3 cases in `tests/unit/test_count_papers_whole_work.py`.

- **#5 `linker.build_edges()` `report.edges_emitted`
  over-counted vs the DB's actual `links` row count.** The
  counter incremented per-append, but the `references` and
  `citations` lists from a provider frequently produce the
  same (src, dst, origin) tuple via different paths
  (A→X listed in A's references AND also in X's citations).
  Downstream `INSERT OR IGNORE` silently collapsed the
  duplicates, so "wrote N edges" was misleadingly high.

  Fix: dedupe at build time via a `_seen` dict keyed by
  `(src_type, src_key, dst_type, dst_key, origin)` — the
  same tuple as the `links` UNIQUE constraint. First-seen
  wins (preserves provenance). `report.edges_emitted =
  len(edges)` at the end matches what will actually land.
  Not data corruption — the `links` table always had
  exactly the right set of unique edges after
  `INSERT OR IGNORE`.

  Locked by 4 cases in `tests/unit/test_linker_edges_dedup.py`:
  same edge seen via both paths counted once, disjoint edges
  all counted, empty cache → 0, unresolved refs count as
  dangling (not emitted).

### Tests

Full venv: 290 → 310 passed (+20 new regression cases).
Stdlib-only CI sim: 255+35 → 260+50 (the 15 new net skips
cover the new tests' optional-dep guards).

### Deferred (unchanged from v0.27.9)

- Big-file split (server.py 2132, import_cmd.py 1505,
  indexer.py 1262, cli.py 1082) — v0.28 scope.
- re_read / re_summarize classifier structural dedup —
  v0.28 scope (two callers still below the abstraction-cost
  threshold).
- 4 remaining marker-constant redeclarations in md_io,
  re_summarize, indexer — v0.28 file-split scope.

## [0.27.9] — 2026-04

Second post-0.27.8 release-hygiene batch. Zero runtime-behaviour
change on happy paths; catches a real install-order bug in
deploy.sh, tightens internal cross-dep pinning so the bundle can
only install as one unified version, consolidates the
skip-guard duplication the previous batch introduced, and
finishes the marker-constant consolidation started back in
0.27.0.

### Fixed

- **`scripts/deploy.sh` install order installed kb_importer
  before kb_write, but kb_importer hard-depends on
  kb-write.** Pre-0.27.9 order
  (core → importer → mcp → write → citations) meant that when
  `pip install -e kb_importer/` ran, its declared
  `kb-write==0.27.8` dep was unsatisfied locally and pip
  would either (a) go to an external index looking for a
  matching version (doesn't exist — the bundle isn't
  published to PyPI) or (b) fail with an unsatisfied-dep
  error. Reordered to a correct topological install:
  `kb_core → kb_write → kb_importer → kb_mcp → kb_citations`.
  Every package is now installed AFTER its deps are
  available locally.

- **Internal cross-dep constraints were too loose for a
  single-bundle release model.** All seven intra-bundle deps
  (`kb-core>=0.27.7`, `kb-write>=0.27.7`, `kb-mcp>=0.27.7`)
  would install with the current VERSION 0.27.8 but also
  accepted arbitrary older bundle versions. That meant a
  user could end up with
  `kb-importer 0.27.8 + kb-write 0.27.7` (say, from a stale
  wheel cache or a partially-failed upgrade) where pip's
  version resolver is technically happy but the inter-
  package API may have drifted. The project's versioning
  rule is explicitly "one unified bundle version; upgrade
  everything together" — tightened all seven cross-deps to
  exact-pin `==0.27.9`. Extended
  `scripts/check_package_consistency.py` to enforce
  cross-dep == VERSION going forward, so future bumps can't
  silently leave this drifted.

- **Three `v0.28.x` version comment markers in code after
  the 0.27.7 / 0.27.8 rollback didn't get renamed.** The
  0.28.0 minor-bump was retracted and turned into the
  0.27.7 patch, but three inline comments still referenced
  `v0.28.0` / `v0.28.1`: `kb_mcp/tools/snapshot.py:250`
  (tar filter introduction — was 0.27.8), migrate_chapters'
  fail-fast-import rationale (was 0.27.7 for the problem +
  0.27.8 for the fix), and `test_migrate_chapters.py`'s
  skip-guard docstring. Updated to the correct when-
  introduced version labels.

- **DEPLOYMENT.md's "expected VERSION file contents" hint
  was still pre-semver.** Two hits (`:145` and `:321`) said
  `a number like 27` dating from the pre-0.27.1 era when
  `VERSION=27`. Replaced with `a semver string like 0.27.9`
  plus an explicit note that all five packages ship as one
  bundle at the same version.

### Code hygiene

- **Consolidated optional-dep skip-guards into
  `tests/conftest.py`.** The 0.27.8 batch added guards to
  prevent stdlib-only CI failures but each of the 7
  affected test files carried its own 5-line
  `_skip_if_no_X()` copy. Moved the three canonical
  helpers (`skip_if_no_mcp`, `skip_if_no_frontmatter`,
  `skip_if_no_httpx`) to conftest as module-level
  functions. Each test file now does
  `from conftest import skip_if_no_X`; the local
  re-declarations are gone.

  Side-effects:
    - `tests/conftest.py` adds `tests/` to `sys.path` so the
      import works from anywhere under `tests/unit/`.
    - `scripts/run_unit_tests.py` explicitly
      `exec_module`s conftest.py once at startup (real
      pytest auto-loads it; this stdlib-only runner didn't
      before). Runs AFTER the vendored pytest stub is
      installed so conftest's `import pytest` resolves.

  Result: 7 test files shrink by ~7 lines each; future
  additions are one-line changes in conftest rather than
  copy-paste.

- **`migrate_chapters` imports marker constants from their
  canonical source.** The four `_`-prefixed inline
  constants (`_FULLTEXT_START`, `_FULLTEXT_END`,
  `_AI_ZONE_START`, `_AI_ZONE_END`) that 0.27.7 shipped —
  intentionally — are now replaced with
  `from kb_core import FULLTEXT_START, FULLTEXT_END` and
  `from ..zones import AI_ZONE_START, AI_ZONE_END`. Drops
  the marker-redeclaration count from 8 to 6 in the
  codebase. The remaining 6 (md_io, indexer,
  re_summarize, the two canonical sources in kb_core and
  kb_write.zones) are on the v0.28 file-split track;
  doing them now would require the structural refactor
  that's been deferred.

### Added

- **Python 3.13 + 3.14 `pyproject.toml` classifiers.** The
  classifier blocks listed up to 3.12; 3.13 test runs were
  already passing in review. Added explicit 3.13 and 3.14
  entries across all five pyproject.toml files. Metadata
  only — no runtime dep change.

### Test infrastructure

- Added the cross-dep pinning lint check (noted in Fixed
  above). The same `scripts/check_package_consistency.py`
  now verifies both version-string alignment AND that
  every intra-bundle cross-dep is exactly
  `==<current VERSION>`.

## [0.27.8] — 2026-04

Release-hygiene pass after 0.27.7. Three consecutive bumps in
0.27.5 / 0.27.6 / 0.27.7 updated the top-level `VERSION` file
and each package's `__init__.__version__` but missed the
`pyproject.toml` `version = "…"` field in every package.
`scripts/check_package_consistency.py` flagged the drift. In
the same review pass, the test-suite skip-guard convention
was found to have been dropped in every test file added since
0.27.5. This release catches up.

(One prior release sequence briefly published a 0.28.0 minor
bump on the migrate-legacy-chapters commit; that was
retracted in 0.27.7, which reassigns that work as a
patch-level batch. See the 0.27.7 entry for the rationale.)

### Fixed

- **`pyproject.toml` versions + cross-deps lagged behind
  `VERSION`/`__version__` by up to three patch bumps.** The
  0.27.5 → 0.27.6 → 0.27.7 chain updated the top-level
  `VERSION` file and each package's `__init__.__version__`,
  but missed the `pyproject.toml` `version = "…"` field in
  all five packages. Cross-dep constraints like
  `"kb-core>=0.27.4"`/`"kb-write>=0.1.0"`/`"kb-mcp>=0.2.0"`
  were also stale. Bumped all five package versions to 0.27.8
  and tightened all internal cross-deps to `>=0.27.7` so the
  bundle is installed as one coherent release, matching the
  intent the inline comments in those files already express.
  Locked by the pre-existing
  `scripts/check_package_consistency.py` lint.

- **Three of the four test files added in
  0.27.5 / 0.27.6 / 0.27.7 lacked the optional-dep
  skip-guard the codebase convention requires.** `test_lazy_reindex_cooldown.py` and
  `test_malloc_trim_cadence.py` transitively
  `import kb_mcp.server`, which module-hard-imports
  `FastMCP` from the `mcp` package.
  `test_migrate_chapters.py` transitively needs
  `python-frontmatter` after this release's fail-fast
  refactor (see below). Without skip-guards these tests turn
  into *failures* in stdlib-only CI runs, violating the
  "`run_unit_tests.py` failure is an event that must block
  release" rule we wrote into the 0.27.4 changelog. Added
  `_skip_if_no_mcp` / `_skip_if_no_frontmatter` helpers
  following the `test_note_kind_compat.py` convention, called
  at the top of every test function (after docstrings where
  present). `test_re_read_classifier.py` was audited,
  confirmed hermetic, and needs no guard.

- **`kb_write.ops.migrate_chapters` masked
  ModuleNotFoundError as "bad frontmatter" per-file.** The
  0.27.7 shape had three in-function `import frontmatter`
  calls inside functions whose callers catch `Exception as e`
  to classify per-file parse failures. When python-frontmatter
  was missing, every legacy chapter generated an identical
  "bad frontmatter: No module named 'frontmatter'" line in
  `report.errors`, making it look like a 182-file data
  corruption when it was actually one missing dep. Moved the
  import to module top as a hard dep with a clear
  `raise ImportError("…install python-frontmatter or install
  kb-importer…")` so the real cause surfaces at command
  invocation. All three in-function imports replaced with
  uses of the module-level `_frontmatter` alias.

- **`kb_mcp.tools.snapshot` passed no `filter=` to
  `tar.extractall`.** Python 3.14 deprecates the no-filter
  default; even on 3.13 the call emitted a DeprecationWarning
  that bled into test runs. Added `filter="data"` as
  belt-and-braces alongside the existing `_is_safe_member`
  pre-filter (which remains the primary defence — it's
  stricter than tarfile's "data" filter, rejecting
  symlinks/hardlinks/devices outright rather than allowing
  relative symlinks).

### Code hygiene

- **`kb_write.ops.migrate_chapters` dropped two unused
  imports** (`WriteExistsError`, `Iterable`) flagged by the
  audit.

### Deferred (flagged again, not addressed this release)

- **`kb_mcp.server` hard-imports `mcp.server.fastmcp` at
  module top** while soft-importing `kb_write`, making
  server-level tests fragile in mcp-less environments. Root
  cause is the server file mixing mcp-protocol layer with
  mcp-free business logic. Fixing cleanly means splitting
  `server.py` into `server_runtime.py` (TTL, trim, state) +
  `server_mcp.py` (FastMCP registration) — v0.28 file-split
  scope per the existing roadmap. The skip-guards added
  this release are the portable workaround until that
  split lands.

- **Big files keep getting bigger** — `server.py` 2132 lines,
  `import_cmd.py` 1505, `indexer.py` 1247, `cli.py` 1082.
  This release adds ~45 lines net (fail-fast import, tar
  filter, skip guards in tests). Still the largest structural
  debt item; on the v0.28 file-split track.

- **`re_read` / `re_summarize` classifiers are near-duplicate**
  logic. Extraction to `kb_core.classifier` would let a
  future third caller avoid a third copy; noted but not done
  (current count is exactly two, cost/benefit doesn't clear
  the bar).

## [0.27.7] — 2026-04

Patch bump. Ships a one-shot data-migration utility for pre-v24
libraries. Treated as patch (not minor) by project convention:
minor bumps are reserved for new modalities or surfaces; a
cleanup tool for legacy data layout stays in the 0.27.x line.

### Added

- **`kb-write migrate-legacy-chapters` one-shot migration.**
  User libraries imported under the pre-v24 longform pipeline
  accumulate chapter mds under
  `thoughts/<date>-<KEY>-ch<NN>-<slug>.md` with
  `kind: thought`. The v26 data model treats chapters as
  first-class paper mds sharing the parent's `zotero_key`,
  at `papers/<KEY>-chNN.md` with `kind: paper`.
  `kb-mcp index-status --deep` has flagged this since v26
  but offered no auto-fix — the field report flagged 182
  orphaned chapters on the 1154-paper test library as a P1
  ask.

  The new subcommand:

    - Scans `thoughts/` for filenames matching
      `<YYYY-MM-DD>-<KEY>-ch<NN>-<slug>.md` where KEY is the
      8-char Zotero-key shape.
    - Filters: requires `kind: thought` +
      (`source_chapter:` or `source_type: book_chapter`) so
      a plain thought whose filename happens to include
      `-chNN-` is NOT rewritten.
    - For each match: write `papers/<KEY>-chNN.md` via
      `atomic_write(create_only)`, preserve body content
      verbatim (no LLM call), then delete the old thought.
    - One batch git commit for the whole run (not 182
      per-file commits).
    - Idempotent: re-runs detect `papers/<KEY>-chNN.md`
      with matching `zotero_key + chapter_number` and skip.
    - Collision: target exists with *different* key/chno →
      skip + report, old thought left in place for user
      inspection.
    - `--dry-run`: print the plan, don't write.

  Live-run on the real 1154-paper library: 182 thoughts →
  182 chapter papers in a single commit, 0 collisions, 0
  errors. `kb-mcp index-status --deep` no longer reports v25
  legacy paths after the migration.

  Locked by 9 cases in `tests/unit/test_migrate_chapters.py`:
  detection (positive + ordinary-thought false-positive
  guard + non-chapter-filename skip), dry-run behaviour,
  idempotency, collision reporting, produced-md canonical
  shape, and body byte-for-byte preservation through
  unicode / formulas / fenced code blocks.

## [0.27.6] — 2026-04

Single bug fix. Brings `re_read`'s failure classifier into
parity with the 0.27.1 upgrade done for `re_summarize`.

### Fixed

- **`re_read` failure classifier over-reported LLM failures.**
  The re-summarize classifier was armored in 0.27.1 (new
  `skip_bad_target` category, preference for
  `exception.code`, broadened substring fallback). The
  re-read classifier — its sibling for the batch path,
  calling `re_summarize` internally and bucketing the
  `ReSummarizeError` it gets back — wasn't updated at the
  same time and therefore over-reported "LLM failures":

    - a batch-selected paper whose md was deleted between
      selection and execution landed in `skip_llm_error`
      (should be `skip_bad_target`)
    - a paper with no `zotero_attachment_keys` in
      frontmatter — the exact v26.5 field report wording —
      landed in `skip_llm_error` (should be
      `skip_pdf_missing`)
    - LLM responses containing "cannot locate" rather than
      "missing"/"not found"/"no pdf" landed in
      `skip_llm_error` (should be `skip_pdf_missing`)

  Fix:

    - `kb_importer/events.py`: new
      `RE_READ_SKIP_BAD_TARGET = "skip_bad_target"`
      category, added to `_ALLOWED_RE_READ_CATEGORIES`.
    - `kb_write/ops/re_read.py`: classifier now recognises
      exception codes
      `no_attachment_keys` → `skip_pdf_missing` and
      `bad_target`/`md_not_found`/`paper_not_found` →
      `skip_bad_target`. Substring fallback now checks
      "paper md not found"/"paper not found"/
      "md … not found" BEFORE the LLM fallback, and the
      PDF-locate branch covers "cannot locate" and
      "no zotero_attachment_keys".
    - `kb_write/selectors/unread_first.py`: `executed_cats`
      (the "attempt was made, don't pick again" set in the
      unread-first selector) gains
      `RE_READ_SKIP_BAD_TARGET` so the selector doesn't
      keep re-picking the same stale key forever.
      Docstring note added: any newly-added
      `RE_READ_SKIP_*` category is an "attempt" by default
      unless explicitly documented otherwise.

  Locked by 15 cases in
  `tests/unit/test_re_read_classifier.py`: 7
  `TestCodeFirst` cases (one per code value + the
  "unknown code defaults to `skip_llm_error`" forward-compat
  invariant), 6 `TestSubstringFallback` cases exactly
  matching the gaps 0.27.5 shipped with, 1 unknown-code-
  no-substring-fallthrough invariant, and 1
  `TestSelectorCompat` verifying the unread-first selector
  sees the new category.

## [0.27.5] — 2026-04

Three bug fixes bundled. No behaviour change on the standard
lock-on write path; improvements land on the edge cases
(concurrent writes, long MCP sessions).

### Fixed

- **`kb_write` auto-commit retry on HEAD ref-lock contention,
  not only `.git/index.lock`.** The 0.27.4 exponential-
  backoff retry matched only `index.lock` phrasings. Git
  holds two serialising locks during a commit: the index
  lock (`.git/index.lock`) during staging and the HEAD ref
  lock (`.git/HEAD.lock`, or `.git/refs/heads/<br>.lock`)
  during the final ref update. Field report at 100-way
  concurrent `kb-write thought create` (via `--no-lock`)
  showed the HEAD-ref lock firing separately with
  `"cannot lock ref 'HEAD': is at <sha> but expected <sha>"`
  — not caught by the old marker list. Added markers
  `"cannot lock ref"` and `"ref lock"` (covers both git
  phrasings). Same retry schedule (0.05 → 0.1 → 0.2 → 0.4s,
  5 attempts ≈ 0.75s total). Locked by two new cases in
  `tests/unit/test_git_lock_retry.py`.

- **`kb_write` auto-commit now scopes each commit to its
  own file via pathspec.** Under concurrent commits without
  kb-write's outer lock, the old shape
  `git add FILE` + `git commit` committed the whole index
  (so a sibling `git add file_S` between our add and commit
  would drag file_S into our commit; the subject
  `create_thought: thoughts/me` became a lie). When a
  sibling committed first our own `git commit` exited
  non-zero with "nothing added to commit" — surfaced as
  `GitError` even though our md was on disk and in git
  under the sibling's commit. Field report at 100-way
  `--no-lock`: 68/100 hit this path, all with mds intact.
  Fix: pass `files` as a pathspec to both
  `git diff --cached --quiet -- FILE` and
  `git commit -m MSG -- FILE`. Each auto-commit is now
  scoped to exactly its caller's file. If a sibling
  committed our file first, the "nothing to commit" /
  "nothing added to commit" / "no changes added to commit"
  wording is swallowed silently (return None). Pre-commit
  hook rejections and other real failures still raise
  `GitError`. `commit_staged()` (used by delete, which
  pre-stages via `git rm`) keeps the whole-index semantics
  by passing no pathspec. Locked by 5 cases in
  `tests/unit/test_auto_commit_pathspec.py`.

- **`kb_mcp.serve` long-session memory growth capped.** The
  0.27.4 CHANGELOG acknowledged "+24 MB RSS / 90 tool
  calls" as "still not profiled, v0.28 scope". tracemalloc
  confirmed Python-object growth per `_lazy_reindex` was
  <10 KB while RSS grew +156 KB/call — the rest is SQLite's
  C-level page + statement cache plus glibc arena retention
  (freed memory the allocator doesn't return to the OS).
  Two bounded-cost levers:

    - `_LAZY_REINDEX_COOLDOWN_S` (default 1.0s, env
      `KB_MCP_LAZY_REINDEX_COOLDOWN_S` override). Back-to-
      back tool calls within an agent burst skip the
      reindex until the cooldown elapses. Failed reindexes
      do NOT stamp the timestamp — otherwise a one-off
      failure would mask subsequent errors across the
      cooldown window.
    - Periodic glibc `malloc_trim(0)` cadence (default
      every 16 `_lazy_reindex` calls, env
      `KB_MCP_MALLOC_TRIM_EVERY` override). Explicitly asks
      glibc to shrink the heap past its high-water mark.
      Non-glibc platforms (musl, macOS) probe the symbol
      once and disable the path; no-op.

  Measured (`mcp-stress` on 1154-paper library, 96 tool
  calls): before +24 MB, linearly growing; after +12.7 MB,
  stable after round 2 (with a reclaim dip at round 8).
  Qualitative change from "linear growth, unbounded" to
  "bounded steady-state with reclaim dips". Locked by 12
  cases across
  `tests/unit/test_lazy_reindex_cooldown.py` and
  `tests/unit/test_malloc_trim_cadence.py`. Remaining
  steady-state baseline (~+12 MB) is mostly SQLite page
  cache and stays in v0.28 scope.

## [0.27.4] — 2026-04

Fifth bug-fix pass. Addresses five items from the v0.27.3 field
report that each turned out to be "I claimed this was fixed in a
prior release but it wasn't." Credit for all findings to direct
library testing.

### Fixed

- **Unit-test runner was killed mid-run by `SystemExit`.**
  `_run_single_case` caught `Exception`, not `BaseException`.
  When a test triggered argparse's `parser.exit()` or called
  `sys.exit()`, the resulting `SystemExit` propagated past the
  runner's handler and terminated the whole process — without
  printing the summary line. The previous release's
  "locked-by-test" claims for multiple items (refresh-counts
  graceful error, note kind compat) were fiction: those tests
  had latent bugs that triggered this path and were invisible
  from the runner output. Now catches `BaseException` (still
  lets `KeyboardInterrupt` propagate), so argparse-style exits
  show up as FAIL instead of silently killing the run. Locked
  by `tests/unit/test_runner_systemexit.py` which spawns a
  subprocess runner against a fake `test_*.py` that raises
  `SystemExit`.

- **`test_note_kind_compat` used an invented `ZoteroItem`
  shape.** Constructed with `extra={}`, `pdf_attachment_key=None`,
  `notes="body"` — none of which are real fields on the
  dataclass. `TypeError` at construction was a silent FAIL
  hidden by the runner bug above. Rewritten against the real
  dataclass shape (notes is `list[ZoteroNote]`, no extra /
  pdf_attachment_key fields).

- **`test_refresh_counts_no_db` used wrong argv order.**
  Put `--kb-root` / `--provider` after the subcommand;
  argparse rejects this at parse time and calls
  `parser.exit(2)` — `SystemExit` — bypassed by the runner
  bug. Argv now puts top-level flags before the subcommand.

- **`test_report_generation` orphans assertion still
  case-sensitive.** v0.27.1 added `"orphan"` as a marker for
  the "found N orphans" report shape, but the actual section
  body in a populated KB is `"## Orphans\n..."` (capital O) and
  `"Archived attachment dirs not referenced by any imported
  md (N)"` — neither contains lowercase `"orphan"` in a
  matchable position without lowercasing. Now matches
  case-insensitive and includes `"archived attachment"` as an
  explicit marker.

- **`kb-mcp serve` SIGTERM handler never fired.** Previous
  implementation called `raise KeyboardInterrupt` inside the
  signal handler, hoping `mcp.run()`'s asyncio loop would catch
  it. In practice MCP stdio transport blocks on `readline()`
  waiting for the next JSON-RPC message, and Python only runs
  signal handlers between bytecode instructions — a blocking
  syscall never returns to bytecode, so the handler never
  executed. Result: `kill -TERM` hung for ~30s, then systemd
  fell back to SIGKILL (exactly what the handler was meant to
  prevent). Now closes the Store inside the handler — SQLite's
  WAL-checkpoint in `store.close()` is synchronous and
  doesn't need Python interpreter state in a good spot — then
  calls `os._exit(0)` to bypass the interpreter entirely.
  SIGINT now goes through the same path for consistency.

- **`KB_WRITE_AUDIT_INCLUDE_USER=1` wrote `"unknown"` in
  common environments.** Fallback chain was `os.getlogin()` →
  `os.environ["USER"] or "unknown"`. In Claude Code / CI
  shells where `$USER` is empty but `$LOGNAME` is set, this
  hit "unknown" in ~1/3 of field environments. Now uses
  `getpass.getuser()` which walks LOGNAME / USER / LNAME /
  USERNAME and falls back to a pwd lookup by euid. Opt-in
  feature now actually works across environments.

### Added (mitigation — not full fix)

- **`.git/index.lock` retry-with-backoff on concurrent writes.**
  50-way parallel `kb-write thought create` with git-commit on
  lost 3/50 commits in the field report (all 50 md files
  landed on disk; only the commit step for 3 collided on the
  index lock). Mitigation added: every `git add` / `git commit`
  / `git diff --cached` goes through `_run_git_with_retry`,
  which retries specifically on index.lock contention markers
  with exponential backoff (0.05s → 0.1s → 0.2s → 0.4s,
  max 5 attempts ≈ 0.75s total). Not a full fix — a truly
  adversarial concurrent workload can still lose a commit —
  but field observation was "3/50 at 50-way, 0/30 at 30-way",
  so a sub-second retry window is expected to cover realistic
  usage. Locked by `tests/unit/test_git_lock_retry.py` with
  injected failures. Full fix (commit-tree + update-ref
  pathway avoiding index.lock entirely) stays in 0.28 scope
  for when someone hits the wall again.

### Test infrastructure

- Runner's monkeypatch already carried the needed features after
  0.27.3 (chdir, setattr); nothing new there. This release is
  pure behaviour fixes.

### Honest retrospective

Three items in this release's list had `CHANGELOG.md` entries
in earlier 0.27.x claiming "locked by tests/unit/X.py". Those
test files existed but never actually ran — the runner-bug +
test-bug interaction meant the claim was unverifiable from the
summary line. The 0.27.4 runner fix is what made the other
four fixes testable. Takeaway: a `scripts/run_unit_tests.py`
failure is an event that must block release, not a line to
read past.

## [0.27.3] — 2026-04

Fixes a real rigidity in workspace autodetect. Previous
versions only looked for the code's own install location
(walking up from `__file__` for a `.ee-kb-tools/` ancestor).
That worked for the deployed layout but left "install from
`~/dev/kb-tools/`, run against `~/research/ee-kb/`" failing
with "could not resolve workspace layout" unless the user
remembered to `export KB_ROOT=...` every session.

### Added — CWD-based autodetect

`find_workspace_root()` now walks up from the user's current
directory looking for any of:

- a directory containing `ee-kb/` (user cd'd to the workspace
  parent — the most common case)
- the `ee-kb/` directory itself (user cd'd into the KB) —
  walks up one more to the parent
- a directory containing `.ee-kb-tools/` (deployed layout,
  still works)

This means the standard day-to-day flow is now:

```bash
# Anywhere on disk — one-time install, code stays put:
cd /path/to/kb-tools
python3 -m venv .venv && source .venv/bin/activate
pip install -e kb_core/ kb_importer/ kb_mcp/ kb_write/ kb_citations/

# Any session thereafter, just cd and run:
cd ~/research            # contains ee-kb/
kb-mcp index             # autodetect finds it from CWD
```

No env vars, no `--kb-root`, no symlinks. Works regardless of
where the source repo lives relative to the KB.

### Precedence, complete list (in order)

1. `--kb-root <path>` / `parent=` arg (explicit)
2. `$KB_WORKSPACE`
3. `$KB_ROOT`
4. **New in 0.27.3**: autodetect from CWD
5. Autodetect from code location (still works when code lives
   under `.ee-kb-tools/`)

### Error message improved

When all five miss, the error now lists four concrete remedies
instead of only mentioning `.ee-kb-tools/` siblings (which was
confusing to users whose source repo wasn't named that).

### Documentation rewrite

`DEVELOPMENT.md` rewritten with zero concept of "dev vs deploy
modes" — there's only one install-and-use flow. `DEPLOYMENT.md`
clarified as a narrower document for the handoff / multi-user
case (staging code into someone else's `.ee-kb-tools/`).

### Code consolidation

`kb_citations/config.py` and `kb_importer/config.py` no longer
carry their own `_find_tools_dir` copies — they delegate to
`kb_core.workspace.find_tools_dir`. Previously four packages
had four copies of the same walk-up loop; v0.27 fixed two,
this release fixes the other two.

### Test infrastructure

- `scripts/run_unit_tests.py` monkeypatch gains `chdir` and
  `setattr` methods (needed to test autodetect behaviour
  deterministically across the sandbox's own install layout).
- `tests/unit/test_workspace_autodetect.py` covers all three
  match shapes + the "nothing found" error shape, mocking out
  code-location autodetect so the test doesn't accidentally
  succeed from the sandbox's own `.ee-kb-tools/` ancestor.

## [0.27.2] — 2026-04

Documentation-only release. Adds a `DEVELOPMENT.md` walkthrough
for the "edit in place" workflow — distinct from the deploy-to-
user scenario already covered by `DEPLOYMENT.md`. README now
points at both.

### Added

- **`DEVELOPMENT.md`** — how to run the code from an in-place
  `kb-tools/` checkout: `pip install -e` inside the repo,
  `export KB_ROOT=...` to point autodetect-less CLI commands at
  your KB, common mistakes / remedies, how the editable install
  interacts with `.egg-info/` (nothing committable).
- **Extended `find_tools_dir` docstring** to document the dev-
  mode caveat: autodetect looks for `.ee-kb-tools/` from the
  code's install location, which means it returns None when
  kb-tools lives in `~/dev/kb-tools/` and the KB lives in
  `~/research/ee-kb/` — two separate trees. That's expected;
  `KB_ROOT` env var is the dev-mode workaround.

### Not changed

- No behaviour changes. Workspace autodetect semantics identical
  to 0.27.1. A brief 0.27.1 experiment that added `kb-tools` as
  a recognised tools-dir name was reverted — the dev workflow
  has code and KB in different parent trees, so autodetect
  doesn't help regardless of what names we recognise. Env var
  is the right abstraction.

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
