<!-- fragment: cross_platform -->
## Cross-platform effects and shared protocol

This KB is shared across multiple agent platforms and editors. Every
change you make is visible to — and consumed by — every other agent
that works here. You are never writing just for your own session.

**Anything you change propagates:**

- A `thought` you create is visible to the next Claude Code session,
  the next opencode session, the MCP client on a different machine,
  and any human editor — immediately on disk, usually within seconds
  after git sync.
- A `.agent-prefs/` rule you add ("always escape LaTeX underscores")
  becomes binding for every future agent, on every platform.
- A `kb_tag` or `kb_ref` you add changes search results and the
  link graph for everyone.
- A file you delete is gone for everyone (git history can recover
  it, but mid-flight agents may break).

**Because every change is shared, every agent must follow the same
protocol.** The protocol is not platform-specific — it is the set
of rules, tools, and conventions documented in this KB:

1. `AGENT-WRITE-RULES.md` — what you can and cannot modify, how to
   handle conflicts, how frontmatter ownership works.
2. `.agent-prefs/` — the user's persistent preferences; you read
   them, you apply them, you extend them via the proper tool.
3. `kb-write` CLI — the only sanctioned way to write to the KB.
   It enforces validation, atomic replace, mtime conflict detection,
   and git auto-commit.
4. `kb-mcp` — the sanctioned way to search and query.
5. The fragments under `kb_write/prompts/fragments/` — the single
   source of truth for all agent entry files. Edit a fragment and
   run `kb-write init --refresh` to propagate to CLAUDE.md /
   AGENTS.md / README.md simultaneously.

**Practical consequences for you:**

- Never invent a KB convention unilaterally. If you think something
  needs a new rule, propose it to the user; if accepted, the user
  or you (via `kb-write`) encodes it in the shared protocol, not
  in ad-hoc session behavior.
- Never assume a file is "yours" just because you created it. The
  moment you commit, it's part of the KB and subject to all shared
  rules.
- If you need to encode platform-specific behavior (e.g. a Claude
  Code-only quirk), do it in your own tool's configuration, not
  in shared files — put it in `~/.claude.md` or similar, not in
  the KB.
- When you update a shared rule, use the shared tool. Editing a
  fragment at `kb_write/prompts/fragments/*.md` and running
  `kb-write init --refresh` is the correct way to update how all
  agents see the rule; hand-editing CLAUDE.md alone means opencode
  keeps the old rule and drift begins.
- Before you write, assume another agent might read your change in
  the next minute. Make the change discoverable (descriptive slugs,
  informative commit messages, clear frontmatter).

In short: you are one of several agents collaborating through the
same filesystem. The KB's conventions exist so you don't have to
guess what the others are doing. Follow them; improve them through
the proper channels.
