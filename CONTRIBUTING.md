# Contributing to ee-kb-tools

Thanks for your interest. This project is a personal research KB
toolchain first, a public utility second — so the bar for new
features is "does it fit the personal workflow described in the
root README?". Bug reports and portability fixes are always
welcome.

## Quickstart

```bash
git clone <repo>
cd ee-kb-tools

# Install the four packages in a single venv
python3 -m venv .venv
source .venv/bin/activate
pip install -e kb_importer/
pip install -e kb_mcp/
pip install -e kb_write/
pip install -e kb_citations/
```

## Project layout

Five Python packages:

- `kb_core/` — shared contract layer (path safety, addressing,
  schema constants, workspace resolution). Zero runtime deps,
  bottom of the dependency DAG.
- `kb_importer/` — Zotero → md importer (metadata + fulltext LLM
  summary pipeline)
- `kb_mcp/` — MCP server and read-only tools over the KB (search,
  graph, citations)
- `kb_write/` — atomic md writes with audit log, plus batch
  re-summarize / re-read
- `kb_citations/` — Semantic Scholar / OpenAlex citation fetcher
  and link builder

Install each as editable:

```bash
pip install -e kb_core/      # must be first
pip install -e kb_importer/
pip install -e kb_mcp/
pip install -e kb_write/
pip install -e kb_citations/
```

Dependency directions (all soft / optional except kb_core):

- `kb_core` → (nothing; stdlib only)
- `kb_importer → kb_core`, `kb_importer → kb_write` (hard;
  atomic_write reuse)
- `kb_citations → kb_core`, `kb_citations → kb_mcp` (hard;
  citation_ops writes to links table)
- `kb_mcp → kb_core`, `kb_mcp → kb_write` (soft; write-tool extras)
- `kb_write → kb_core`, `kb_write → kb_importer` (soft,
  function-local; re-summarize / re-read / events recording)

Do **not** introduce new cross-package imports at module
top-level unless kb_core is the target. Within-bundle soft
dependencies follow the existing "function-local import with
ImportError fallback" pattern.

## Required checks before PR

All of these must pass locally:

```bash
# 1. Package consistency (version, cross-package duplicated files)
python3 scripts/check_package_consistency.py

# 2. No system-path autodetect leaking into logs / errors
python3 scripts/check_no_system_paths.py

# 3. No secrets, no personal info, no CJK in code/comments
python3 scripts/check_no_secrets.py

# 4. End-to-end tests (no network required; ~5s)
python3 scripts/test_e2e.py

# 5. Byte-compile sanity
python3 -m compileall -q kb_importer/src kb_mcp/src kb_write/src kb_citations/src
```

## Style

- **Language**: code and comments must be English. LLM prompts and
  the constants used to parse LLM output may carry CJK (see
  `scripts/check_no_secrets.py` for the exempt file list).
- **Docstrings**: every public function gets one. Google-style or
  plain prose; whichever fits. Explain *why*, not just *what*.
- **Comments**: favour explanatory comments that capture design
  rationale. `# obvious restatement of code` comments are noise;
  `# this guard exists because commit 3f8a hit race X` is gold.
- **Line length**: soft cap at 80. Hard cap at 100 (prefer to break
  rather than run long).
- **No new third-party runtime deps** without discussion; they make
  the 4-pkg independent-install design harder to maintain.

## Commit / PR

- Small PRs preferred. One behaviour change per PR.
- Commit messages: imperative mood ("fix X", not "fixed X"),
  72-char subject, body wrapping at 72.
- Link issues in the body: `Closes #NN`.
- Update relevant README sections and `scripts/test_e2e.py` in
  the same PR as the behaviour change.

## Adding a new MCP tool

1. Implement in `kb_mcp/src/kb_mcp/tools/<name>.py`.
2. Register in `kb_mcp/src/kb_mcp/server.py` with `@mcp.tool()` and
   a docstring that tells the agent when to use it (not just what
   it does).
3. Add to the tool count assertion in `scripts/test_e2e.py`.
4. Update `kb_mcp/README.md` MCP-tools table.

## Adding a new selector for `kb-write re-read`

1. Create `kb_write/src/kb_write/selectors/<name>.py` with a class
   that satisfies the `Selector` Protocol in `base.py`. Declare
   `ACCEPTED_KWARGS = frozenset({...})` (empty if none).
2. Register in `selectors/registry.py`.
3. Add a row to the README selectors table.
4. Add a case to `test_selectors_basic` / `test_selectors_robustness`.

## Releasing

Before producing a release zip or pushing to a public repo:

1. Bump `VERSION` at repo root.
2. Update `__version__` in each package's `__init__.py` to match.
3. Add a section to `CHANGELOG.md` summarising the window.
4. Run all 5 checks above. Commit the release with message
   `release: vX.Y`.
