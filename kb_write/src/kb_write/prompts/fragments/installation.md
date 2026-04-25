<!-- fragment: installation -->
## Installation and configuration

You are likely already in a correctly-configured workspace — if
`kb-mcp`, `kb-write`, `kb-importer`, and `kb-citations` all work
when invoked from the user's shell, you can skip this section. If
any CLI is missing or config is broken, use this as the source of
truth.

### Expected layout

```
<parent>/                          # parent dir, any name
├── .ee-kb-tools/
│   ├── kb_importer/ kb_mcp/ kb_write/ kb_citations/   # source
│   ├── .venv/                                          # virtualenv
│   └── config/
│       ├── kb-mcp.yaml
│       ├── kb-importer.yaml
│       ├── kb-citations.yaml
│       └── README.md
├── ee-kb/                         # this knowledge base
└── zotero/storage/                # Zotero attachments (PDFs)
```

Tools autodetect this layout by walking up from their install
location to `.ee-kb-tools/`, then reading sibling directories.
Never assume absolute paths; always rely on autodetect or the
user-provided `--kb-root` / `$KB_ROOT`.

### First-time install

```bash
cd <parent>/.ee-kb-tools
python -m venv .venv
source .venv/bin/activate

# Topological order: kb_core → kb_write → kb_importer → kb_mcp → kb_citations
pip install -e ./kb_core
pip install -e ./kb_write
pip install -e ./kb_importer
pip install -e "./kb_mcp[gemini]"   # or without [gemini] for openai-only
pip install -e ./kb_citations

# Scaffold the KB (creates CLAUDE.md / AGENTS.md / .cursorrules /
# .aiderrc / AGENT-WRITE-RULES.md + .agent-prefs/ + 4 config files
# in .ee-kb-tools/config/)
kb-write init
```

Editable installs (`-e`) are intentional: edits to source files
take effect immediately, no rebuild.

### API keys — strict policy

API keys live **only** in the user's shell rc (`~/.bashrc` /
`~/.zshrc`), as exported environment variables. **Never** put them
in YAML config files, never commit them to git, never write them
to `.agent-prefs/`.

```bash
# ~/.bashrc
export OPENAI_API_KEY=sk-...               # for OpenAI embeddings
export GEMINI_API_KEY=...                  # for Gemini embeddings
export ZOTERO_API_KEY=...                  # only for kb-importer web mode
export SEMANTIC_SCHOLAR_API_KEY=...        # optional; raises S2 rate limit
```

YAML files reference the env var **by name** (`api_key_env:
OPENAI_API_KEY`), never the value. If a user asks you to "put my
API key in the config," refuse and explain — the correct place is
the shell rc.

### Configuration files

All config is in `.ee-kb-tools/config/`. Nothing anywhere else —
no `~/.config/`, no `~/.local/share/`, no `/etc/`.

**`kb-mcp.yaml`** — embedding provider, indexer settings:

```yaml
embeddings:
  provider: openai           # or: gemini
  # model / dim / batch_size all optional

  # OpenAI-compatible endpoint (Ollama, vLLM, DashScope, etc.):
  # openai_base_url: http://localhost:11434/v1
  # model: nomic-embed-text
  # dim: 768
```

Switching provider doesn't require rebuilding the vector index as
long as the dimension matches (both openai and gemini default to
1536 dim).

DeepSeek does **not** have an embedding API — do not set
`openai_base_url` to `api.deepseek.com` for embeddings; it will
404. DeepSeek only supports chat-mode via its own endpoint; that's
orthogonal to kb-mcp's embedding layer.

**`kb-importer.yaml`** — Zotero sync source:

```yaml
zotero:
  source_mode: live          # or: web
  library_id: ""             # required iff source_mode == web
  library_type: user         # or: group
  api_key_env: ZOTERO_API_KEY
```

`source_mode: live` reads from a running local Zotero app via its
HTTP API at `localhost:23119` (no network, no key needed).
`source_mode: web` reads from `api.zotero.org` (needs `library_id`
and the key in `$ZOTERO_API_KEY`).

**`kb-citations.yaml`** — citation graph source:

```yaml
provider: semantic_scholar   # or: openalex
max_refs: 1000
max_cites: 200
freshness_days: 30
fetch_citations: false       # if true, also pull incoming citations
```

OpenAlex requires `mailto:` for the polite pool (email, not
secret; can live in YAML or `$OPENALEX_MAILTO`).

### Provider precedence (memorize this chain)

Every tool resolves its settings in this order; use it when
debugging unexpected behavior:

1. **CLI flag** (`--kb-root`, `--provider`, ...) — absolute override
2. **Environment variable** (`$KB_ROOT`, `$OPENAI_API_KEY`, ...)
3. **YAML file** in `.ee-kb-tools/config/`
4. **Workspace autodetect** (sibling `ee-kb/` of `.ee-kb-tools/`)
5. Built-in defaults

If an agent is surprised by a value, trace through the chain from
1 to 5 before assuming a bug.

### Runtime state lives with the KB

```
ee-kb/.kb-mcp/
├── index.sqlite           # FTS5 + vector + link graph
├── write.lock             # kb-write advisory lock
├── audit.log              # every successful write, JSON Lines
└── citations/             # kb-citations per-paper JSON cache
    └── by-paper/<key>.json
```

This is **state**, not config, so it lives with the content
(inside `ee-kb/`) rather than in `.ee-kb-tools/config/`. Don't
move it. Users who back up / sync `ee-kb/` get the index backed up
automatically.

### Verifying the install

After any install or config change, run:

```bash
python <parent>/.ee-kb-tools/scripts/post_install_test.py
```

This runs 16 smoke tests: CLI PATH checks, init, write operations
(create/update/audit/dry-run/doctor/log), `kb-mcp index`, API
connectivity (OpenAI/Gemini/S2; skipped if no key), and the no-
system-path lint. Use `--workspace <parent>` to test against the
actual workspace instead of a throwaway temp dir.

Exit 0 = healthy. Non-zero = something to fix before relying on
the tools.
