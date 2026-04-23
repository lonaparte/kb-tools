# kb-core

Shared contract layer for the `ee-kb` toolchain. Extracted in v27
to replace the previous "mirrored files held in sync by a lint"
pattern between `kb_write` and `kb_mcp`.

## Scope

`kb_core` holds **only** the things every ee-kb package needs to
agree on:

- **Path layout** — kb-relative paths (`papers/`, `topics/`,
  `thoughts/`, `.agent-prefs/`), the v26 book-chapter filename
  convention (`<KEY>-chNN.md`), the `.kb-mcp/` subpath constants.
- **`safe_resolve`** — the canonical "resolve a kb-relative path
  against kb_root, reject escapes" function. Used by every package
  that accepts a user-provided path.
- **Workspace autodetect** — walking up from the current directory
  to find the `.ee-kb-tools/` sibling of `ee-kb/`.
- **Schema / format version constants** — `SCHEMA_VERSION = 7`,
  events file name, audit file name, fulltext marker strings.

## Non-scope (strictly)

`kb_core` does **not** contain:

- Business logic (no md parsing, no git, no LLM calls, no DB
  access). Those live in their respective packages.
- Anything that imports from another ee-kb package. `kb_core` is
  the root of the dependency DAG.
- Third-party runtime dependencies. Pure stdlib.

This keeps `kb_core` tiny, testable in isolation, and safe to pin
strictly across the other packages.

## Install

Normally pulled in transitively; explicit install only for
development:

```bash
pip install -e kb_core/
```

`kb_write`, `kb_mcp`, `kb_importer`, and `kb_citations` all depend
on `kb-core>=0.1`.

## Versioning

Semver inside v27: additions bump MINOR, breaking changes bump
MAJOR. Since every other package pins kb-core, a MAJOR bump means
coordinated update across the bundle — treat it as you would a
schema version bump.
