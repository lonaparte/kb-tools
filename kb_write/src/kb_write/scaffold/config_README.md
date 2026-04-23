# .ee-kb-tools/config/

All configuration for the ee-kb toolchain lives here. Nothing is
stored under `~/.config/`, `~/.local/share/`, or any other system
path.

## Files

| File                  | Consumer       | Required? |
|-----------------------|----------------|-----------|
| `kb-mcp.yaml`         | `kb-mcp`       | No — all fields have defaults |
| `kb-importer.yaml`    | `kb-importer`  | Typically yes — Zotero userID needs to be set here |
| `README.md`           | humans         | This file |

## What does NOT go here

**API keys.** Never put `OPENAI_API_KEY`, `GEMINI_API_KEY`, or
Zotero API keys into these files. They belong in your shell rc
(`~/.bashrc` / `~/.zshrc`) as exported environment variables:

```bash
# in ~/.bashrc
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...
export ZOTERO_API_KEY=...    # only if you use kb-importer with Zotero web API
```

The YAML files reference keys by env var name, never store them.

**Runtime state.** Lock files, audit logs, SQLite index, citation
cache — those all live in `<ee-kb>/.kb-mcp/`, not here. Config is
static; state is dynamic.

**User preferences.** Per-session agent behavior (writing style,
research context, etc.) goes in `<ee-kb>/.agent-prefs/`, not here.
Those are content, not tool config.

## Precedence

All tools follow this order when resolving any setting:

1. CLI flag (`--kb-root`, `--config`, etc.)
2. Environment variable (`$KB_ROOT`, `$KB_MCP_CONFIG`, etc.)
3. This YAML file
4. Workspace autodetect / compiled-in default

This lets you override any setting per-invocation without editing
the YAML, and lets CI pipelines run entirely off environment vars
without file config at all.

## Git-tracking

These files SHOULD be committed to git (they contain your chosen
defaults, not secrets). The `.ee-kb-tools/` parent directory itself
is usually a separate git repo from `ee-kb/`.

## Editing

Edit with any text editor. Changes take effect on the next tool
invocation — no reload needed.
