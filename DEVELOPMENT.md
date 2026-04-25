# Using kb-tools

One flow, one mental model. Code lives somewhere, your KB lives
somewhere, the CLI connects the two. It doesn't matter whether
they're in the same parent directory or on opposite sides of
your disk.

## Install once

```bash
cd /path/to/kb-tools           # wherever you put the repo
python3 -m venv .venv
source .venv/bin/activate
pip install -e kb_core/ kb_write/ kb_importer/ kb_mcp/ kb_citations/
```

Topological order: `kb_core → kb_write → kb_importer → kb_mcp →
kb_citations`. `kb_core` first because the other four pin it as a
versioned dep; `kb_write` next because `kb_importer` hard-depends
on it; `kb_citations` last because it needs `kb_mcp` (soft, `[link]`).

The venv and `*.egg-info/` are `.gitignore`d. `pip install -e`
writes a `.pth` file into the venv's `site-packages/` pointing at
your `src/` dirs, so editing a `.py` file takes effect
immediately. You only re-run `pip install -e` if you change a
`pyproject.toml` (new dep, new entry point, version bump) or
rename / delete a top-level module file.

## Run commands against your KB

The CLI needs to know WHICH KB to operate on. It tries these in
order, stopping at the first match:

1. **`--kb-root <path>`** on the command — per-command override.
2. **`$KB_ROOT`** env var — session-wide.
3. **`$KB_WORKSPACE`** env var — points at the parent directory
   containing `ee-kb/`.
4. **Autodetect from CWD.** Walks up from the current directory
   looking for a dir that is (or contains) `ee-kb/`. Handles the
   common case: you `cd` into your workspace (or into `ee-kb/`
   itself) and run the command.
5. **Autodetect from code location.** Only fires when the code
   itself lives under a `.ee-kb-tools/` directory — the layout
   `scripts/deploy.sh` produces. Irrelevant when the source repo
   sits elsewhere.

If all five miss, the error message lists the four things you
can do.

## Common setups

### A. Code next to the KB

```
~/research/
├── kb-tools/       ← source repo (you)
├── ee-kb/          ← your knowledge base
└── zotero/
```

```bash
source ~/research/kb-tools/.venv/bin/activate
cd ~/research
kb-mcp index              # autodetect #4 finds ee-kb/ in CWD
```

No env vars needed. This is the simplest setup.

### B. Code elsewhere, KB in a fixed location

```
~/dev/kb-tools/     ← code, you work on it from here
~/research/         ← workspace
├── ee-kb/
└── zotero/
```

```bash
source ~/dev/kb-tools/.venv/bin/activate
cd ~/research
kb-mcp index              # autodetect #4 finds ee-kb/ in CWD
```

Still no env vars. You can even `cd ~/research/ee-kb/papers/`
and autodetect still walks up until it finds the KB.

### C. Running from anywhere

If you want to run CLI commands without first `cd`-ing into
your workspace:

```bash
# ~/.bashrc (or ~/.zshrc)
export KB_ROOT=$HOME/research/ee-kb
source $HOME/dev/kb-tools/.venv/bin/activate
```

Now `kb-mcp index` works from any directory.

### D. Multiple KBs

Don't put `KB_ROOT` in your rc. Instead, set it per-project or
rely on autodetect — `cd ~/research && kb-mcp ...` uses the
research KB, `cd ~/side-project && kb-mcp ...` uses the other
one. No config drift risk.

### E. Deploy to someone else's machine

When the code should live WITH the KB (e.g. you're handing a
package to another user), use `scripts/deploy.sh`:

```bash
cd /path/to/kb-tools
./scripts/deploy.sh ~/research
```

This copies the code into `~/research/.ee-kb-tools/`, builds a
venv there, and installs. See `DEPLOYMENT.md` for the full
walkthrough and LLM-agent-readable step list.

## Daily workflow

```bash
# One-time per day (or add to rc):
source /path/to/kb-tools/.venv/bin/activate

# Edit code:
cd /path/to/kb-tools
# … edit … save …
python3 scripts/run_unit_tests.py
python3 scripts/test_e2e.py

# Run against your KB:
cd ~/research      # or wherever ee-kb/ lives
kb-mcp index
kb-write re-summarize 5N6FQXJJ
```

Any change you save in the source repo is live on the next
CLI invocation.

## Common mistakes

- **"command not found: kb-mcp"** — venv not activated. Run
  `source /path/to/kb-tools/.venv/bin/activate` or check
  `which python` points inside your venv.
- **"could not resolve workspace layout"** — neither CWD nor
  any env var got the CLI to a valid KB. Either `cd` into
  your workspace or `export KB_ROOT=...`.
- **CLI picks up the wrong KB** — usually a stale `$KB_ROOT` in
  your shell rc. `echo $KB_ROOT` to check; `unset KB_ROOT` to
  fall back to autodetect.
- **`ModuleNotFoundError: kb_core`** — you installed other
  packages but forgot `kb_core`. Run `pip install -e kb_core/`.
- **My change isn't taking effect** — stale `__pycache__/`
  after a big rename. `find . -name __pycache__ -exec rm -rf
  {} +` from inside the repo.
- **Tests run fine locally, fail in CI** — CI scripts should
  `export KB_ROOT=...` explicitly rather than trust autodetect;
  CI runs in `/tmp` or similar where there's no `ee-kb/`
  ancestor.

## Sanity check for agents

Before running any command, verify the CLI will find the right
KB:

```bash
python3 -c "
from kb_core.workspace import resolve_workspace
ws = resolve_workspace()
print(f'kb_root = {ws.kb_root}')
print(f'tools_dir = {ws.tools_dir}')
"
```

If `kb_root` isn't what you expect, fix `$KB_ROOT` or `cd`
before proceeding.

## What pip install -e leaves in the repo

Per package: `<pkg>/src/<pkg>.egg-info/` — metadata only, no
code. All covered by `.gitignore` (`*.egg-info/`). Nothing
committable. The actual "editable" part (the path to your
`src/` dirs) lives in the venv's `site-packages/`, outside
the repo.
