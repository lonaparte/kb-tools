# Upgrading ee-kb-tools

A practical guide for moving an existing workspace from one version of
ee-kb-tools to a newer one. Covers the canonical path (pattern A, below)
and the fallback (pattern B), plus the few cases where a version bump
requires more than a `git pull`.

If you're setting up for the first time, see
[DEPLOYMENT.md](DEPLOYMENT.md). This file is only for existing
workspaces.

## TL;DR (pattern A — most common)

```bash
cd <workspace-parent>/.ee-kb-tools

# 1. Read what's changed since your current version.
cat CHANGELOG.md | less              # scan for ### Breaking blocks

# 2. Snapshot the projection DB before upgrading (cheap insurance).
cd ../ee-kb && kb-mcp snapshot export "../pre-upgrade-$(date +%F).tar.gz"
cd ../.ee-kb-tools

# 3. Pull the new code + re-sync deps.
git fetch
git log --oneline HEAD..origin/main  # preview the bump
git pull

source .venv/bin/activate
pip install -e ./kb_core                  # always first
pip install -e ./kb_write
pip install -e "./kb_mcp[write,gemini]"
pip install -e ./kb_importer
pip install -e ./kb_citations

# 4. Verify the toolchain is coherent at the new version.
python scripts/post_install_test.py

# 5. If the CHANGELOG flagged a schema bump, reindex.
cd ../ee-kb && kb-mcp reindex --force
```

That's the common case. Sections below explain each step, what to do
when something breaks, and which versions actually require extra work.

## Before you upgrade

1. **Note your current version.** `cat .ee-kb-tools/VERSION` or
   `kb-write --version`. You need this to know which CHANGELOG
   entries to read.
2. **Snapshot first.** `kb-mcp snapshot export path.tar.gz` dumps
   the projection DB + vector caches. If the upgrade breaks something
   you can `kb-mcp snapshot import path.tar.gz` to restore — faster
   than a reindex. The md files themselves are git-tracked separately
   in `ee-kb/`; they don't need a snapshot.
3. **Commit or stash any in-flight edits in `ee-kb/`.** Nothing in
   the toolchain modifies pending git state, but a clean working
   tree makes "did upgrade X break my files?" trivial to answer.

## Pattern A: `.ee-kb-tools/` is its own git clone

If you followed DEPLOYMENT.md's pattern A (the recommended default),
`.ee-kb-tools/` is a git clone of kb-tools. Upgrading is `git pull`
plus a pip re-sync.

```bash
cd <workspace-parent>/.ee-kb-tools
git fetch
git log --oneline HEAD..origin/main              # preview
git pull
source .venv/bin/activate
pip install -e ./kb_core                         # order matters
pip install -e ./kb_write
pip install -e "./kb_mcp[write,gemini]"
pip install -e ./kb_importer
pip install -e ./kb_citations
python scripts/post_install_test.py
```

**Why reinstall if editable?** Editable installs pick up `.py` edits
immediately — no reinstall needed for normal code changes. But
`pyproject.toml` changes (new dep, new optional extra, changed pin)
need a reinstall to take effect. Running the five `pip install -e`
commands is the safest default. Skip it only if you've read the diff
and verified no dependency churn.

**Why `kb_core` first?** The other four packages pin `kb-core==<version>`
as a hard dependency. Without the local editable install on the path
first, pip would try to resolve that pin from PyPI and fail. This
is a pip quirk, not an ee-kb design choice.

## Pattern B: re-deploy from a kept-elsewhere clone

If `.ee-kb-tools/` was populated by `scripts/deploy.sh` from a clone
that lives outside `<workspace-parent>/`, the flow is:

```bash
cd /path/to/your/kb-tools-clone
git pull

# Preserve the old venv (faster than reinstalling from scratch).
mv <workspace-parent>/.ee-kb-tools/.venv /tmp/keep-venv

# Replace the code.
rm -rf <workspace-parent>/.ee-kb-tools
./scripts/deploy.sh <workspace-parent>

# Restore the venv, then re-sync pins.
rm -rf <workspace-parent>/.ee-kb-tools/.venv
mv /tmp/keep-venv <workspace-parent>/.ee-kb-tools/.venv
source <workspace-parent>/.ee-kb-tools/.venv/bin/activate
pip install -e <workspace-parent>/.ee-kb-tools/kb_core
pip install -e <workspace-parent>/.ee-kb-tools/kb_write
pip install -e "<workspace-parent>/.ee-kb-tools/kb_mcp[write,gemini]"
pip install -e <workspace-parent>/.ee-kb-tools/kb_importer
pip install -e <workspace-parent>/.ee-kb-tools/kb_citations
python <workspace-parent>/.ee-kb-tools/scripts/post_install_test.py
```

This is more work than pattern A; prefer pattern A unless you need
the source-of-truth checkout separate from the deployment.

## Schema bumps

The projection DB (`ee-kb/.kb-mcp/index.sqlite`) carries a schema
version. When the code's expected version is higher than the DB's
stamped version, kb-mcp's `ensure_schema()` auto-drops the tables
and applies the new schema on the next run — no manual migration
step. But the tables are then **empty**, so content has to be
rebuilt by `kb-mcp index` or `kb-mcp reindex --force`.

Practical upgrade flow when a schema bump is involved:

```bash
# After pip install -e ... has landed the new code:
cd <workspace-parent>/ee-kb
kb-mcp reindex --force      # clears embedding cache too; slowest path
# OR
kb-mcp index                # incremental — re-embeds everything because
                            # the freshly-dropped DB sees every md as new
```

`reindex --force` is the safe default. Use `index` only if you know
the embedding cache under `.kb-mcp/embeddings/` is still valid for
the new schema — usually it is, since chunks are content-addressed.

**How to know a version bump crosses a schema bump:** search the
CHANGELOG span between your old and new version for either:

- `schema v<N> → v<N+1>` or `schema bumped to v<N>`
- `reindex --force required`
- Anything in a `### Breaking` block under an `ee-kb/.kb-mcp/`
  context

If none of those appear, your DB keeps working untouched and you can
skip this section entirely.

Schema history at a glance (consult CHANGELOG for the full rationale):

| DB schema | Version that bumped it | What changed |
|-----------|------------------------|--------------|
| v6 → v7   | 0.27.0                 | Fixed foreign-key targets on `paper_attachments` / `paper_tags` / `paper_collections` / `paper_chunk_meta` — v6 pointed them at a non-unique column and INSERTs were silently failing. |
| v5 → v6   | v26 (first public release) | Papers PK changed to `paper_key` for the chapter-as-paper refactor. |

The authoritative current version is `kb_core.schema.SCHEMA_VERSION`;
a cross-package consistency lint enforces that every package sees
the same number.

## Config migrations

Config files under `.ee-kb-tools/config/` are YAML. New releases
occasionally:

- **Add a new key** with a code default. Your old config continues
  working unchanged; the new feature uses the default until you opt
  in by editing the file.
- **Rename a key** with a deprecation warning. kb-importer emits a
  `DeprecationWarning` for the old name, but reads both. Update when
  convenient.
- **Flip a default.** Example: 0.28.0 changed `source_mode` default
  from `live` to `web`. Existing configs are unaffected (the key
  was explicitly set). Only the `scaffold/` template default moved.
- **Add a new scaffold file.** When kb-write adds a new
  `config_kb_*.yaml` scaffold, existing workspaces don't
  automatically get it — the scaffold is only written by
  `kb-write init` on a fresh workspace. Re-running `kb-write init`
  on an existing workspace is safe: it refuses to overwrite
  existing files, but writes any new ones that are missing.

**To check whether your configs are up to date** after an upgrade:

```bash
cd <workspace-parent>/ee-kb
kb-write init              # idempotent — only fills missing files
ls ../.ee-kb-tools/config/ # compare against the current scaffold set
diff <(kb-importer show-template) ... # (for config template comparison)
```

## Rollback

If the upgrade goes wrong and you want back to where you were:

### Pattern A rollback

```bash
cd <workspace-parent>/.ee-kb-tools
git reflog                 # find the commit you were on
git reset --hard <old-sha>
source .venv/bin/activate
pip install -e ./kb_core
pip install -e ./kb_write
pip install -e "./kb_mcp[write,gemini]"
pip install -e ./kb_importer
pip install -e ./kb_citations
```

If the rollback crosses a schema bump, also:

```bash
cd <workspace-parent>/ee-kb
kb-mcp snapshot import <pre-upgrade-snapshot.tar.gz> --force
```

`--force` is needed because `index.sqlite` already exists at the
newer schema — the old code refuses to open it (a kb-mcp older than
the DB gets a `schema version N is newer than this code supports`
error). Snapshot import overwrites the DB with the old-schema copy,
which the rolled-back code can read.

### Pattern B rollback

Reverse the `git pull` in your off-site clone, rerun deploy + pip
install:

```bash
cd /path/to/kb-tools-clone
git reset --hard <old-sha>
# Then redo Pattern B upgrade flow.
```

## After the upgrade — validation

1. `kb-write --version` / `kb-mcp --version` / `kb-importer --version`
   / `kb-citations --version` — all four should report the same
   version and match `cat .ee-kb-tools/VERSION`.
2. `python scripts/post_install_test.py` — runs 16 smoke tests. API
   tests skip without keys; that's normal. Any failure in the
   non-API sections means the install is incomplete — redo pip
   install.
3. `kb-mcp index-status` — should print the DB path + staleness.
4. `kb-write doctor` — scans the KB for rule violations. Should
   usually be clean.
5. If you reindexed, spot-check a search: `kb-mcp index` again (now
   a no-op), then a known-title query via your MCP client.

## Troubleshooting

- **`command not found: kb-importer`** — the venv isn't active.
  `source .ee-kb-tools/.venv/bin/activate`.
- **`ModuleNotFoundError: No module named 'kb_core'`** — the
  reinstall order was wrong. Do `kb_core` first.
- **`No matching distribution found for kb-core==X.Y.Z`** during
  pip install — you skipped `pip install -e ./kb_core`. That command
  must run before any of the other four pins can resolve.
- **`zotero_storage_dir is required` after upgrading from 0.29.3
  or earlier** — this is the bug fixed in 0.29.5. Upgrade to
  0.29.5+ or set `KB_ZOTERO_STORAGE` + `KB_ROOT` in your shell.
- **Old import-summary mds look wrong** — probably a fulltext
  marker bug from a pre-v22 release. See the v22 CHANGELOG entry;
  the `inject_fulltext` surgical splice introduced there is
  idempotent — just re-run `kb-importer import papers --force-fulltext`
  on the affected keys.
- **Upgrade crossed a schema bump but you forgot to reindex** —
  `kb-mcp index-status` prints an explicit "schema mismatch"
  error; `kb-mcp reindex --force` fixes it.

## Upgrade lessons recorded

Every surprise that affected an upgrade ends up documented in
CHANGELOG.md. If you hit something not covered here, check there
first — specifically the "Fixed" and "### Breaking" blocks in the
version span you're upgrading across.
