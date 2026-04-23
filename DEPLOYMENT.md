# Deployment guide

> For the **general install-and-use flow** (what most people
> including contributors want), read [`DEVELOPMENT.md`](DEVELOPMENT.md).
> That's the "install once, run from CWD" flow and it works
> whether you're actively editing the code or just using it.
>
> This file is narrower: it's about putting the code into
> SOMEONE ELSE'S workspace — e.g. an LLM agent setting up a
> new user's machine from a handoff. It uses `scripts/deploy.sh`
> to stage the code inside a `.ee-kb-tools/` directory next to
> the user's `ee-kb/`.

This document explains how to turn a cloned `kb-tools` repo into a
working install on another machine. **It is written to be read by
an LLM agent acting on the user's behalf, not (only) by a human** —
so it's explicit about filesystem paths, commands to run, and
verification steps.

If you're an LLM agent: follow the steps in order. After each
checkpoint, verify the expected state before continuing. On any
unexpected result, stop and report to the user rather than
improvising.

## Prerequisites

The user must already have:

- Python 3.10 or newer (`python3 --version` → 3.10+)
- `pip` that corresponds to that Python
- A directory layout like this (after deployment is done):

  ```
  <workspace-parent>/
  ├── .ee-kb-tools/      ← created by this guide; contains the code
  ├── ee-kb/             ← the knowledge base (may be empty initially)
  └── zotero/            ← Zotero data directory
      └── storage/       ← Zotero attachment store
  ```

  `<workspace-parent>` can be named anything (`research/`,
  `workspace/`, etc.) — it's identified by containing the three
  sibling directories above.

- The cloned `kb-tools` repo sitting **as a sibling** of
  `<workspace-parent>` (or anywhere else — it doesn't matter;
  after deployment we delete it).

## What "deployment" means

Turn this:
```
somewhere/
└── kb-tools/           ← you are here; just cloned
    ├── kb_core/
    ├── kb_write/
    ├── kb_mcp/
    ├── kb_importer/
    ├── kb_citations/
    └── ...
```

into this:
```
<workspace-parent>/
├── .ee-kb-tools/       ← contents of kb-tools, COPIED here
│   ├── .venv/          ← new Python virtualenv
│   ├── kb_core/
│   ├── kb_write/
│   └── ... (all code)
├── ee-kb/              (pre-existing or newly initialised)
└── zotero/             (pre-existing — managed by Zotero itself)
```

and then `kb-tools/` can be deleted.

The user's shell activates the `.venv` inside `.ee-kb-tools/` to
get the `kb-importer`, `kb-mcp`, `kb-write`, `kb-citations`
commands on PATH.

## Steps

### 1. Confirm workspace-parent with the user

Ask the user where `<workspace-parent>` is on disk. Common answers:

- `~/research/` (laptop)
- `/srv/kb/` (home-server)

Do **not** guess. Once confirmed, use the exported variable
`WORKSPACE_PARENT` in the rest of these commands.

```bash
export WORKSPACE_PARENT=/absolute/path/confirmed/by/user
```

Verify it exists and contains `ee-kb/` and `zotero/`:

```bash
test -d "$WORKSPACE_PARENT/ee-kb" || echo "MISSING: ee-kb/"
test -d "$WORKSPACE_PARENT/zotero" || echo "MISSING: zotero/"
```

If `ee-kb/` is missing, offer to create it (see step 6). If
`zotero/` is missing, stop — Zotero setup is outside this guide's
scope.

### 2. Refuse to clobber existing `.ee-kb-tools/`

```bash
test -d "$WORKSPACE_PARENT/.ee-kb-tools" && echo "ALREADY EXISTS: $WORKSPACE_PARENT/.ee-kb-tools"
```

If it exists, stop and ask the user whether to (a) back it up and
replace, (b) skip deployment (already installed), or (c) abort.
Do NOT silently merge — the `.venv` inside a stale copy will
break in confusing ways after a pip reinstall.

### 3. Copy the code

From inside the cloned repo (`kb-tools/`):

```bash
# Assuming `pwd` is kb-tools/ (the repo)
rsync -a --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
      ./  "$WORKSPACE_PARENT/.ee-kb-tools/"
```

`rsync` is the right call here because it handles symlinks, mode
bits, and progress reporting. If `rsync` is unavailable on the
platform (rare on Linux/macOS; common on stripped-down Windows),
fall back to:

```bash
cp -a .  "$WORKSPACE_PARENT/.ee-kb-tools/"
find "$WORKSPACE_PARENT/.ee-kb-tools/" -name __pycache__ -type d -exec rm -rf {} +
rm -rf "$WORKSPACE_PARENT/.ee-kb-tools/.git"
```

Verify the copy landed:

```bash
test -f "$WORKSPACE_PARENT/.ee-kb-tools/VERSION" || echo "COPY FAILED"
cat "$WORKSPACE_PARENT/.ee-kb-tools/VERSION"
# Expected: a semver string like 0.27.9 (all five packages are
# released together at the same version; cross-deps inside
# pyproject.toml are pinned to match).
```

### 4. Create the virtualenv inside .ee-kb-tools/

```bash
cd "$WORKSPACE_PARENT/.ee-kb-tools"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 5. Install the five packages (order matters)

`kb_core` is the dependency root. All other packages import from
it, so it must go in first. The bundle packages are editable
installs so users get updates via `cd .ee-kb-tools && git pull`
(once kb-tools is set up with the deployment repo as origin).

```bash
# kb_core first
pip install -e kb_core/

# Then the four bundle packages
pip install -e kb_importer/
pip install -e kb_mcp/
pip install -e kb_write/
pip install -e kb_citations/
```

After each install, the corresponding command should be on PATH:

```bash
which kb-importer  kb-mcp  kb-write  kb-citations
# All four should print paths under .ee-kb-tools/.venv/bin/
```

### 6. Initialise the KB (optional — only if ee-kb/ was empty)

If step 1 found `ee-kb/` missing or empty, scaffold it now:

```bash
cd "$WORKSPACE_PARENT"
mkdir -p ee-kb
kb-write init --kb-root "$WORKSPACE_PARENT/ee-kb"
```

This creates the `papers/`, `topics/`, `thoughts/`, `.kb-mcp/`,
and `.agent-prefs/` subdirs plus the AGENT-WRITE-RULES.md file.

### 7. Run the post-install sanity check

From anywhere inside the workspace:

```bash
cd "$WORKSPACE_PARENT"
python3 "$WORKSPACE_PARENT/.ee-kb-tools/scripts/post_install_test.py"
```

Expected: a handful of "✓" lines and "post-install check passed".
Any "✗" line is a real problem — don't proceed until it's
understood.

### 8. Configure for the user's setup

Edit (or create) `"$WORKSPACE_PARENT/.ee-kb-tools/config/kb-importer.yaml"`
and `kb-mcp.yaml`. Ask the user for:

- Zotero source mode: `live` (Zotero desktop running + local API
  enabled) or `web` (Zotero cloud via API key).
- If `web`: Zotero API key (stored in `ZOTERO_API_KEY` env, not
  in config file).
- LLM provider (OpenAI / Gemini / DeepSeek) and API key (env
  vars: `OPENAI_API_KEY` etc.).

Template configs live in `.ee-kb-tools/config/`; copy and edit.

### 9. Run the first import (optional but recommended)

```bash
cd "$WORKSPACE_PARENT"
kb-importer import papers --limit 5 --dry-run
```

If that prints 5 candidate papers, run without `--dry-run` to
actually import. Don't do a full-library import on the first try
— validate on 5 first.

### 10. Delete the source kb-tools/ repo

Once steps 1–9 succeeded, the `kb-tools/` directory from the
original `git clone` is no longer needed. The code now lives in
`.ee-kb-tools/` and updates via `cd .ee-kb-tools && git pull`
(after setting up `.ee-kb-tools/` as its own git clone — see
note below on updates).

```bash
rm -rf /path/to/where/you/cloned/kb-tools
```

## Updating later

Two valid patterns:

### Pattern A: `.ee-kb-tools/` as its own git repo

After step 3, before step 4, run:

```bash
cd "$WORKSPACE_PARENT/.ee-kb-tools"
git init
git remote add origin <original-kb-tools-clone-url>
git fetch
git reset --hard origin/main   # or whichever branch you cloned
```

Then future updates are `git pull` inside `.ee-kb-tools/`.
**Caveat:** `pip install -e` pins editable installs against the
src-layout inside `.ee-kb-tools/`. Ordinary `git pull` of a
non-schema, non-pyproject change takes effect immediately. For
dependency changes (`pyproject.toml` edits), re-run step 5.

### Pattern B: re-deploy from a fresh kb-tools clone

Keep the `kb-tools/` clone around somewhere (not inside
`<workspace-parent>`). On update:

```bash
cd /somewhere/kb-tools
git pull
# Back up existing .ee-kb-tools/.venv (keep)
mv "$WORKSPACE_PARENT/.ee-kb-tools" "$WORKSPACE_PARENT/.ee-kb-tools.bak"
# Re-run steps 3-5. The .venv can be reused by copying it back
# into the fresh .ee-kb-tools/ after step 3.
```

Pattern A is cleaner for the common case. Pattern B is better if
you want the source-of-truth checkout to be separate from the
deployment (e.g. running multiple KB instances from one source).

## .gitignore for workspace-parent

If the user version-controls `<workspace-parent>` itself (common
for the KB — `ee-kb/` is worth tracking), add to its `.gitignore`:

```
.ee-kb-tools/
ee-kb/.kb-mcp/
```

- `.ee-kb-tools/` is code + venv; version-controlled separately
  (pattern A) or regenerated (pattern B).
- `ee-kb/.kb-mcp/` is derived data: the projection DB, events
  log, audit log, snapshots. All rebuildable from the md files.

## Troubleshooting the LLM is likely to hit

- **"command not found: kb-mcp"** — user didn't activate the
  venv. `source $WORKSPACE_PARENT/.ee-kb-tools/.venv/bin/activate`.
- **"no KB root configured"** — user ran a command from outside
  `<workspace-parent>` without setting `$KB_ROOT`. Either `cd`
  to the workspace or `export KB_ROOT=$WORKSPACE_PARENT/ee-kb`.
- **"python-frontmatter not installed"** — pip install didn't
  complete. Re-run step 5 with verbose output to see the failure.
- **sqlite-vec build error on install** — needs C compiler. On
  Debian/Ubuntu: `apt install build-essential`. On macOS: Xcode
  command-line tools.
- **mcp package not found** — the `mcp` PyPI package name
  conflicts with older packages on some mirrors. If
  `pip install mcp>=1.0.0` fails, try
  `pip install --index-url https://pypi.org/simple/ "mcp>=1.0.0"`.

## Summary checklist for the LLM

Before reporting "deployment done" to the user, verify:

- [ ] `$WORKSPACE_PARENT/.ee-kb-tools/VERSION` exists and contains a
      semver string (e.g. `0.27.9`). All five packages are released
      together at this version.
- [ ] `$WORKSPACE_PARENT/.ee-kb-tools/.venv/bin/kb-mcp` exists
- [ ] `python3 $WORKSPACE_PARENT/.ee-kb-tools/scripts/post_install_test.py` prints "passed"
- [ ] `$WORKSPACE_PARENT/ee-kb/` exists with subdirs
      (`papers`, `topics`, `thoughts`, `.kb-mcp`)
- [ ] User has been asked about LLM provider + Zotero setup
      (step 8)
- [ ] User knows to `source .venv/bin/activate` or add the venv's
      bin dir to their shell rc

Then, and only then, offer to delete `kb-tools/`.
