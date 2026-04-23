# kb-write

Client-agnostic write layer for the `ee-kb` knowledge base. One set
of rules, one validation pipeline, one atomic-write path — used by
both local agents (Claude Code, opencode, scripts) via CLI and by
MCP-based agents (Claude Desktop) via `kb-mcp` tool wrappers.

## Why this package exists

The KB is shared across:
- **Local agents** that read/write the filesystem directly (Claude Code,
  opencode, plain scripts, your own editor)
- **MCP clients** that reach the KB through `kb-mcp`'s tool calls

Without a shared write layer, each client would enforce its own
version of "what's allowed" — and `kb-importer`'s Zotero sync would
silently revert any edit that crossed the wrong line. This package
is the shared contract.

## What it enforces

Every write operation:

1. **Validates** against the rules in `AGENT-WRITE-RULES.md`
   (frontmatter ownership, AI-zone markers, slug conventions).
2. **Guards mtime** — if the file changed between your read and your
   write, you get a `WriteConflictError` instead of silent clobber.
3. **Writes atomically** via temp-file + `os.replace`.
4. **Acquires a write lock** (`.kb-mcp/write.lock`) so two
   `kb-write` processes can't interleave.
5. **Git-commits** the change by default (can disable per-call).
6. **Triggers `kb-mcp index`** so search/graph update immediately.

## Install

```bash
cd kb_write
pip install -e .
```

Lightweight deps: `python-frontmatter`, `PyYAML`. No SQLite, no MCP,
no OpenAI — those belong to `kb-mcp`.

## CLI

```bash
# Scaffold a KB (idempotent). Also the entry point for new repos.
kb-write init [--refresh] [--force]

# Thoughts and topics (full-file ownership)
kb-write thought create --title "..." --body-file /tmp/x.md \
  [--ref papers/KEY]... [--tag t1]...
kb-write thought update thoughts/2026-04-22-foo \
  --expected-mtime 1234.5 --body-file /tmp/x.md [--title ...] ...
kb-write topic create --slug NAME --title "..." --body-file ...
kb-write topic update topics/NAME --expected-mtime ... ...

# Paper/note AI zone (only the zone between markers)
kb-write ai-zone show papers/KEY
kb-write ai-zone update papers/KEY --expected-mtime ... --body-file ...

# Tags / refs (any md)
kb-write tag add papers/KEY --tag to-reread
kb-write tag remove papers/KEY --tag stale
kb-write ref add thoughts/SLUG --ref papers/KEY
kb-write ref remove thoughts/SLUG --ref papers/KEY

# Preferences (.agent-prefs/)
kb-write pref add --slug writing-style --scope writing --body-file ...
kb-write pref update writing-style --expected-mtime ... --body-file ...
kb-write pref list
kb-write pref show writing-style           # prints one pref
kb-write pref show                         # dumps all prefs formatted for agent

# Delete (thought/topic/preference only)
kb-write delete thoughts/SLUG --yes
kb-write delete .agent-prefs/some-pref --yes

# Audit log
kb-write log                               # recent 20 ops
kb-write log -n 100 --op create_thought    # filter by op
kb-write log --actor mcp                   # only MCP-originated writes

# Diagnostics
kb-write rules                             # prints AGENT-WRITE-RULES.md
kb-write doctor                            # scans for violations
kb-write doctor --fix                      # auto-repair safe issues

# Re-summarize (single paper): re-run the 7-section summariser and
# splice results where the new pass judges the stored text wrong.
kb-write re-summarize papers/KEY
kb-write re-summarize papers/KEY --provider gemini --model ...
kb-write re-summarize papers/BOOKKEY-ch03         # book chapter md

# Re-read (batch): pick N papers via a pluggable selector and run
# re-summarize on each. Every outcome is logged to events.jsonl so
# `kb-mcp report` can aggregate.
kb-write re-read                                  # default: 5 papers, unread-first
kb-write re-read --count 10 --selector stale-first
kb-write re-read --selector by-tag --selector-arg tag=foundational
kb-write re-read --selector related-to-recent \
                 --selector-arg anchor_days=7
kb-write re-read --dry-run-select                 # cheap preview; no LLM
kb-write re-read --list-selectors                 # show all strategies
kb-write re-read --source storage                 # only papers with PDF on disk
kb-write re-read --seed 42                        # reproducible selection

# One-shot migration of pre-v24 longform chapters from
#   thoughts/<date>-<KEY>-ch<NN>-<slug>.md   (kind: thought)
# into the v26 canonical location
#   papers/<KEY>-chNN.md                     (kind: paper)
# Preserves body content verbatim (no LLM call), idempotent
# (re-runs skip already-migrated chapters), reports collisions
# without overwriting. All moves land in one batch git commit.
kb-write migrate-legacy-chapters --dry-run         # preview the plan
kb-write migrate-legacy-chapters                   # actually migrate
```

## dry-run mode

Pass `--dry-run` to any write subcommand to see what would happen
without touching the filesystem:

```bash
# Show the preview of a would-be-created thought
kb-write --dry-run thought create --title "idea" --body-file b.md

# Show a unified diff of an update
kb-write --dry-run thought update thoughts/2026-04-22-foo \
    --expected-mtime 1745... --body-file new.md
```

dry-run shows `git diff`-style output for updates and a
line-numbered preview for creations; for deletes, a one-line
warning. JSON mode (`--json`) emits the diff/preview as structured
fields.

## Audit log

Every successful write op appends a JSON line to
`<kb_root>/.kb-mcp/audit.log` (unless `audit=False` in the
WriteContext). Fields per line:

```json
{"ts":"2026-04-22T14:02:11.234Z","actor":"cli",
 "op":"create_thought","target":"thoughts/2026-04-22-foo.md",
 "mtime_after":1745414531.234,"git_sha":"abc123",
 "reindexed":true,"pid":12345,"user":"joel"}
```

- `actor`: `"cli"` | `"mcp"` | `"python"` — who drove the write
- `mtime_before` / `mtime_after`: conflict-guard audit
- `git_sha`: commit hash if auto-commit ran
- `user`: OS login name

Read with `kb-write log` (human) or `kb-write --json log` (scripts).
Filters: `--op <name>`, `--actor <cli|mcp|python>`, `-n <count>`.

Failures in audit logging are silently swallowed — audit must never
block a write.

## Generated entry files (single source of truth)

Agent entry files at the KB root are **generated** from fragments
under `kb_write/prompts/fragments/`. `kb-write init` produces five
of them simultaneously:

| File            | Read by                       |
|-----------------|-------------------------------|
| `README.md`     | humans + generic agents       |
| `CLAUDE.md`     | Claude Code                   |
| `AGENTS.md`     | opencode (and similar)        |
| `.cursorrules`  | Cursor                        |
| `.aiderrc`      | Aider (`aider --read`)        |

Edit a fragment, run:

```bash
kb-write init --refresh
```

— all five update from the same source. Any content you appended
AFTER the `<!-- kb-write generated end -->` marker is preserved.

Global flags (apply to any subcommand):

| Flag                | Effect                                          |
|---------------------|-------------------------------------------------|
| `--kb-root PATH`    | KB location (default: `$KB_ROOT`).              |
| `--no-git-commit`   | Skip git auto-commit for this operation.        |
| `--no-reindex`      | Skip `kb-mcp index` after writing.              |
| `--no-lock`         | Skip write lock (only for debugging).           |
| `--dry-run`         | Validate but don't write.                       |
| `--commit-message`  | Extra body for the git commit message.          |
| `--json`            | Emit machine-readable JSON on stdout.           |

Exit codes:
- `0` — success
- `2` — CLI error (missing flag, etc.)
- `3` — rule violation
- `4` — mtime conflict (retry after re-reading)
- `5` — create target already exists
- `6` — AI-zone marker malformed
- `7` — path outside KB
- `10` — unexpected internal error

## Python API

```python
from kb_write.config import WriteContext
from kb_write.ops import thought, topic, ai_zone, tag, ref, preference, delete, doctor

ctx = WriteContext(kb_root="/path/to/kb", git_commit=True, reindex=True)

# Create a thought
r = thought.create(ctx, title="An idea", body="...", refs=["papers/X"])
print(r.address.md_rel_path)    # thoughts/2026-04-22-an-idea.md
print(r.mtime)                   # use as expected_mtime next time

# Update
r = thought.update(ctx, "thoughts/2026-04-22-an-idea",
                   expected_mtime=r.mtime, body="new content")

# AI zone — v26: append-only (was v25 full-replace update)
body, mtime = ai_zone.read_zone(ctx.kb_root, "papers/ABCD1234")
r = ai_zone.append(ctx, "papers/ABCD1234",
                   title="connection to X", body_md="Bullet...",
                   date="2026-04-22")

# Tags / refs (low-stakes; mtime optional)
tag.add(ctx, "papers/ABCD1234", "to-reread")
ref.add(ctx, "thoughts/slug", "papers/ABCD1234")

# Re-summarize (single paper, v26)
from kb_write.ops import re_summarize as rs
report = rs.re_summarize(ctx, "papers/ABCD1234")
print(rs.format_report(report))

# Re-read (batch, v26.x) — selectors chosen by name from registry
from kb_write.ops.re_read import re_read, format_report
report = re_read(
    ctx, count=5,
    source_name="papers",
    selector_name="unread-first",
    seed=42,
)
print(format_report(report))

# Doctor
from kb_write.ops import doctor as doc
report = doc.doctor(ctx)
print(doc.format_report(report))
```

### Extending the selector framework (v26.x)

`kb-write re-read` uses a pluggable selector registry so you can
add new selection strategies without touching the re-read command.
Each selector is one file under `kb_write/selectors/`:

```python
# kb_write/selectors/my_selector.py
from pathlib import Path
from .base import PaperInfo


class MySelector:
    name = "my-strategy"
    description = "Pick papers by <your rule>."

    def select(
        self, candidates: list[PaperInfo], *,
        count: int, kb_root: Path,
        seed: int | None = None, **kwargs,
    ) -> list[str]:
        # Return up to `count` paper_keys from candidates.
        return [c.paper_key for c in candidates[:count]]
```

Then register in `kb_write/selectors/registry.py`:

```python
from .my_selector import MySelector

REGISTRY = {
    # ...existing...
    "my-strategy": MySelector(),
}
```

That's it — `kb-write re-read --selector my-strategy` now works,
and `--list-selectors` picks it up automatically. `--selector-arg
key=value` pairs are forwarded to your `select()` as `**kwargs`
(as strings — coerce as needed).

Seven selectors ship built-in: `unread-first` (default), `random`,
`stale-first`, `never-summarized`, `oldest-summary-first`,
`by-tag`, `related-to-recent`. See each file in
`kb_write/selectors/` for the contract.

## See also

- `AGENT-WRITE-RULES.md` — the normative contract every write
  respects.
- `kb_mcp/README.md` — how to query the KB.
- `kb_importer/README.md` — how papers get into the KB from Zotero.
