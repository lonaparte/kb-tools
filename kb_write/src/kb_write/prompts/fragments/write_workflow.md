<!-- fragment: write_workflow -->
## How to write here

Do NOT edit KB files with your raw edit/write tool. Use the `kb-write`
CLI instead — it enforces AI-zone markers, frontmatter ownership,
mtime conflict detection, atomic replace, and auto-commits to git:

```bash
kb-write thought create --title "..." --body-file /tmp/x.md
kb-write thought update thoughts/2026-04-22-foo --expected-mtime ... --body-file ...
kb-write topic create --slug ... --body-file ...
kb-write ai-zone append papers/ABCD1234 --expected-mtime ... --title "..." --body-file ...
kb-write tag add papers/ABCD1234 --tag to-reread
kb-write ref add thoughts/2026-04-22-foo --ref papers/ABCD1234
kb-write pref add --slug writing-style --scope writing --body-file ...
kb-write doctor                 # scan for rule violations
```

Run `kb-write --help` for the full list.

### Flags an agent must NOT use without explicit human approval

These flags exist for human-driven debugging or operator override.
They sidestep the safety properties kb-write normally enforces, so
an agent calling them silently can lose data, leak secrets, or
corrupt the KB:

- `--no-lock` — skips the multi-process write lock. Concurrent
  kb-write runs (you + a cron job + an MCP server) can then race
  and lose writes. Only valid for single-process debugging by a
  human.
- `--no-git-commit` — skips the auto-commit. The md is on disk
  but isn't in git history. If the next kb-write run does commit,
  your prior change gets bundled into someone else's commit
  message. Only valid when a human is staging changes manually.
- `--no-reindex` — kb-mcp's projection lags reality. Search /
  graph queries return stale results until the next manual index.
  Acceptable for batch ops where you'll reindex at the end, but
  agents should generally let kb-write index per write.
- Raw `git` commands targeting KB files — bypass every check
  above. If the operation isn't expressible via kb-write, ask the
  human first.
- `openai_base_url` / `openrouter_base_url` pointing anywhere
  other than the official endpoint or `localhost`. These send
  the configured API key to the URL you point at. Only change
  these if the human explicitly configured a self-hosted gateway.

If a human asks you to use any of the above, do it; the issue is
agents reaching for them on their own initiative to "make things
work" when the safer path errors out.
