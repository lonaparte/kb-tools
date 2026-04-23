# Agent Preferences

This directory stores the user's persistent quirks, style rules, and
context that every agent (Claude Code, opencode, MCP clients, local
CLI tools, etc.) should honor across sessions.

These files are **not** thoughts, notes, or topics — they're
meta-instructions about how the agent should behave. They live in a
dotted directory (`.agent-prefs/`) so kb-importer and kb-mcp indexers
skip them automatically. Only agents reading the filesystem directly
see them.

## How each file works

Each file is a Markdown document with YAML frontmatter. Example:

```yaml
---
scope: writing
priority: 50
last_updated: 2026-04-22
---
# Writing style

- Escape `_` in LaTeX math: write `$H\_k$` not `$H_k$`
- Avoid em-dashes; prefer parenthetical asides
- Keep paragraphs short when explaining technical points
```

| Frontmatter field | Meaning                                               |
|-------------------|-------------------------------------------------------|
| `scope`           | When this pref applies: `global`, `writing`, `research`, `ai-summary`, etc. Use any slug that makes sense. |
| `priority`        | Integer 0-100. Higher wins on conflict. Default 50.   |
| `last_updated`    | ISO date. Agents should also consider mtime.          |

## Recommended file layout (start with these; add more as needed)

| Filename                | scope         | Typical contents                          |
|-------------------------|---------------|-------------------------------------------|
| `global.md`             | global        | Environment (OS, which machine), cross-cutting defaults, "always reply in Chinese", etc. |
| `writing-style.md`      | writing       | Formatting, math notation, paragraphing   |
| `research-context.md`   | research      | Your research field, methods, what you care about |
| `ai-summary.md`         | ai-summary    | Rules specifically for generating AI paper summaries |

Feel free to create additional files like `code-style.md`, `debug.md`,
`communication.md`. Naming convention: kebab-case slugs.

## Precedence when prefs conflict

1. **In-conversation direct instructions** always win (the user just
   told you something NOW).
2. Scope-specific pref (e.g. `ai-summary.md` when doing a summary)
   beats `global.md`.
3. Within the same scope, higher `priority` wins.
4. Within same scope AND same priority, newer `last_updated` wins.
5. `AGENT-WRITE-RULES.md` is NOT a preference — it's a rule. It
   always beats prefs on write-safety matters; prefs can't weaken
   the rules.

## Who writes these files

- **The user** edits them with any text editor. They are
  git-versioned; change history is kept.
- **`kb-write pref`** CLI subcommand — see `kb-write pref --help`.
- **Agents** may propose new prefs or updates when the user says
  things like "from now on...", "always...", "I prefer...". Use
  `kb-write pref add` or `kb-write pref update` and commit the
  change — the user can review via `git log` or `git diff`.

## How agents should use these

1. At the **start of every session** where you'll touch the KB or do
   substantive work with the user, read every `.md` file here.
2. Apply prefs silently — don't narrate "I see you prefer X, I'll do
   X"; just do X. Only bring prefs up if there's a conflict or
   ambiguity.
3. When the user states a new preference in conversation, confirm
   briefly and propose adding it:
   > "Got it. Want me to save that to .agent-prefs/writing-style.md
   > so it persists?"
   Then, if yes, use `kb-write pref update`.
4. If you find the same preference stated in two places with
   different wording, flag it to the user and suggest
   consolidating — don't guess which is newer.
