<!-- fragment: search_hint -->
## How to read / search

Prefer `kb-mcp` over grep for content search — it has FTS5, vector
similarity, and link-graph awareness:

```bash
kb-mcp index-status     # see what's indexed
kb-mcp index            # refresh after edits
```

MCP tools available (in MCP contexts): `search_papers_hybrid`,
`related_papers`, `backlinks`, `trace_links`, `search_papers_fts`,
`find_paper_by_attachment_key`, `find_paper_by_key`, `list_files`,
`read_md`, `grep_md`, `index_status`, `get_agent_preferences`.

For non-MCP contexts (plain shell), `cat` / `grep` / `find` work on
the md files — but remember `.kb-mcp/index.sqlite` gets stale after
edits; run `kb-mcp index` to refresh.
