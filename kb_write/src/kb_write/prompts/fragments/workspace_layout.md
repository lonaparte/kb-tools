<!-- fragment: workspace_layout -->
## Workspace layout and configuration policy

This KB assumes the **canonical three-sibling layout**:

```
<parent>/
├── .ee-kb-tools/
│   ├── kb_importer/ kb_mcp/ kb_write/   # tool source code
│   ├── .venv/                           # virtualenv
│   └── config/                          # ★ ALL tool configuration
│       ├── kb-mcp.yaml
│       ├── kb-importer.yaml
│       └── README.md
├── ee-kb/                               # this knowledge base
│   ├── papers/ topics/standalone-note/ topics/agent-created/ thoughts/
│   ├── .agent-prefs/                    # user preferences (content)
│   ├── .kb-mcp/                         # runtime state + cache
│   │   ├── index.sqlite
│   │   ├── write.lock
│   │   ├── audit.log
│   │   ├── events.jsonl                 # v26: fulltext skips + re-read outcomes
│   │   └── citations/
│   └── CLAUDE.md / AGENTS.md / ...
└── zotero/storage/                      # Zotero attachments
```

The parent directory can be named anything. Tools find each other
by the sibling relationship, not by absolute path.

### Strict configuration policy

| Lives in                          | What                                     |
|-----------------------------------|------------------------------------------|
| `.ee-kb-tools/config/*.yaml`      | Tool configuration (chosen defaults)     |
| `ee-kb/.agent-prefs/*.md`         | User preferences (content, versioned)    |
| `ee-kb/.kb-mcp/`                  | Runtime state (sqlite, lock, logs, cache)|
| `~/.bashrc` / `~/.zshrc`          | API keys (env vars only)                 |
| **nowhere under `~/.config/`**    | (system paths are deliberately unused)   |

API keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ZOTERO_API_KEY`) are
env vars, set in your shell rc. The YAML files reference them by
name, never store them.

### Switching embedding provider

Edit `.ee-kb-tools/config/kb-mcp.yaml`:

```yaml
embeddings:
  provider: gemini    # or: openai
```

Both providers output 1536-dim vectors by default, so switching
doesn't require rebuilding the vector index.
