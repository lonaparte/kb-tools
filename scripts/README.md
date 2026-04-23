# scripts/

Dev and post-install utilities for ee-kb-tools.

## post_install_test.py

Smoke test to run after `pip install` on your real machine. Covers:

- All 4 CLIs (`kb-importer`, `kb-mcp`, `kb-write`, `kb-citations`)
  are on PATH and print `--help`
- `kb-write init` scaffolds 5 entry files + 4 config files
- `kb-write` operations work end-to-end (create, update, audit
  log, dry-run diff, doctor, log)
- `kb-mcp index` runs on a fresh KB
- API connectivity for OpenAI, Gemini, Semantic Scholar (skipped
  if the respective `*_API_KEY` env var isn't set)
- The no-system-path lint passes

Usage:

```bash
# Throwaway workspace (default — created + cleaned up)
python scripts/post_install_test.py

# Keep the workspace around for debugging
python scripts/post_install_test.py --keep-workspace

# Run against your real workspace
python scripts/post_install_test.py --workspace /path/to/workspace-parent
```

Exit codes:
- 0: all passed
- 1: some test failed
- 2: environment problem (missing dependency, workspace not found)

API tests are always non-failing — if you don't have a key for
provider X, it shows as SKIP and doesn't affect the exit code.

## check_no_system_paths.py

AST-based lint that enforces the strict configuration policy:
tools never autodetect `~/.config/`, `$XDG_CONFIG_HOME`,
`/etc/ee-kb*`, or any similar system path. Run after any code
change touching config loading:

```bash
python scripts/check_no_system_paths.py
```

Exit 0 clean, 1 if violations found. See the script header for
what patterns it flags.
