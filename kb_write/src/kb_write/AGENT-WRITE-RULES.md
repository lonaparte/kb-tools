# AGENT-WRITE-RULES

> **Anyone (human or agent) writing to `ee-kb/` must follow these rules.**
> They exist to prevent the two biggest failure modes: data loss from
> concurrent edits, and kb-importer's Zotero sync overwriting your
> changes silently.

This document is shipped with the `kb-write` package and lives at the
root of the `ee-kb/` repo. Read it once, then trust the `kb-write`
CLI (or `kb_write.ops.*` Python API) to enforce most rules for you.

---

## 0. Workspace layout and configuration policy

The canonical directory layout is three sibling directories under
any parent:

```
<parent>/
├── .ee-kb-tools/
│   ├── kb_importer/ kb_mcp/ kb_write/   # tool source code
│   ├── .venv/                           # virtualenv
│   └── config/                          # ★ all tool config
│       ├── kb-mcp.yaml
│       ├── kb-importer.yaml
│       └── README.md
├── ee-kb/                               # this knowledge base
│   ├── papers/                          # external (incl. book chapters)
│   ├── topics/standalone-note/          # Zotero standalone notes (v26)
│   ├── topics/agent-created/            # AI topic syntheses (v26)
│   ├── thoughts/                        # AI dated thoughts
│   ├── .agent-prefs/                    # user preferences (content)
│   ├── .kb-mcp/                         # runtime state + cache
│   └── CLAUDE.md / AGENTS.md / ...
└── zotero/storage/                      # Zotero attachments
```

The parent may be named anything. `ee-kb/` and `.ee-kb-tools/` as
siblings is what tools rely on. Everything autodetects from this
structure; `$KB_WORKSPACE`, `$KB_ROOT`, or `--kb-root` can override.

### Strict configuration policy

**All tool configuration lives under `.ee-kb-tools/config/`.** No
files are read from or written to `~/.config/`, `~/.local/share/`,
`/etc/`, or any other system path. This is deliberate — the workspace
is self-contained and portable.

| Category                          | Location                                 |
|-----------------------------------|------------------------------------------|
| Tool configuration                | `.ee-kb-tools/config/*.yaml`             |
| User preferences (content)        | `ee-kb/.agent-prefs/*.md`                |
| Runtime state (sqlite, logs, cache)| `ee-kb/.kb-mcp/`                        |
| API keys                          | env vars in `~/.bashrc` / `~/.zshrc`     |

API keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ZOTERO_API_KEY`) are
exported in your shell rc and referenced **by variable name** in the
YAML configs, never stored in them.

---

## 1. Mental model

The KB is a Markdown-backed store with four node types. **v26
directory layout** (breaking change from v25 — see top-level
README for migration):

| Directory                        | Node type | Primary author            | AI may edit?           |
|----------------------------------|-----------|---------------------------|------------------------|
| `papers/`                        | paper     | kb-importer (from Zotero) | Only the AI zone       |
| `topics/standalone-note/`        | note      | kb-importer (from Zotero) | Only the AI zone       |
| `topics/agent-created/`          | topic     | **agent / human**         | All of it              |
| `thoughts/`                      | thought   | **agent / human**         | All of it              |

Papers and notes are **mirrors** of external (Zotero) state. Topics
and thoughts are **yours**. The AI zone inside a paper/note is the
narrow region where you can add analytical content tied to that
specific paper/note.

**Book / long-article chapters (v26).** A Zotero item that kb-importer
processes with the long-form pipeline produces several paper mds
under `papers/` — the whole work at `papers/<KEY>.md` plus one
sibling per chapter at `papers/<KEY>-chNN.md`. All are `kind: paper`
and **share the same `zotero_key`** in frontmatter. To find all
parts of a multi-md work, call the MCP tool
`list_paper_parts(zotero_key)`. `find_paper_by_key` returns only
the whole-work md.

**Legacy v25 paths (deprecated, NOT indexed).** If content exists
at these legacy paths, `kb-mcp index-status` flags it. Move it to
the v26 location or delete it:

| v25 path (not indexed)                        | v26 replacement              |
|-----------------------------------------------|------------------------------|
| `zotero-notes/<KEY>.md`                       | `topics/standalone-note/<KEY>.md` |
| `topics/<SLUG>.md` (top-level)                | `topics/agent-created/<SLUG>.md`  |
| `thoughts/<date>-<KEY>-chNN-*.md`             | `papers/<KEY>-chNN.md`            |

---

## 2. The zones (absolute rules)

Every `papers/*.md` file has this structure (enforced by kb-importer):

```markdown
---
<frontmatter — see §3>
---

# <title>

## Abstract
<!-- zotero-field: abstractNote -->
<content from Zotero — read-only>

## Zotero Notes
<content from Zotero child notes — read-only>

## Attachments
<list of PDFs — read-only>

<!-- kb-fulltext-start -->
<AI summary from external processing — kb-importer owns this region>
<!-- kb-fulltext-end -->

<!-- kb-ai-zone-start -->
<YOUR SPACE — freely editable>
<!-- kb-ai-zone-end -->
```

**Rules**:

1. **The `<!-- kb-ai-zone-start -->` / `<!-- kb-ai-zone-end -->` markers
   must never be deleted, reordered, or duplicated.** Content between
   them can be anything you want. `kb-write` refuses to write if
   markers are missing or mismatched.

2. **Content outside the AI zone in papers/notes is read-only to you.**
   That includes Abstract, Zotero Notes, Attachments, and the
   kb-fulltext region. Editing them is guaranteed to be overwritten
   on the next `kb-importer sync`.

3. **The kb-fulltext region is owned by kb-importer.** It gets
   populated by `kb-importer import-summaries` from external AI
   summaries. Do not touch it via `kb-write`; use `kb-importer
   set-summary` instead.

4. **Topics and thoughts have no zones — the whole file is yours.**
   You can edit frontmatter freely (except reserved fields in §3).

---

## 3. Frontmatter field ownership

Every md has YAML frontmatter at the top. Fields are split by owner:

| Prefix / name                   | Owner       | Rule                                   |
|---------------------------------|-------------|----------------------------------------|
| `zotero_*`                      | kb-importer | Never modify — silent overwrite risk.  |
| `fulltext_*`                    | kb-importer | Never modify — internal state flags.   |
| `zotero_main_attachment_key`    | kb-importer | Override possible, but see §3.1.       |
| `kind`, `item_type`, `title`, `authors`, `year`, `doi`, `publication`, `citation_key`, `abstract` | kb-importer | Never modify. |
| `kb_tags` (list of strings)     | agent       | Merge-append, dedupe. Don't replace.   |
| `kb_refs` (list of paths)       | agent       | Merge-append, dedupe. Don't replace.   |
| `kb_notes` (free text)          | agent       | Free edit.                             |
| `kb_*` (any other `kb_` prefix) | agent       | Free. Convention: `snake_case`.        |

### 3.1 Overriding `zotero_main_attachment_key`

`kb-importer` picks a heuristic "main PDF" among several attachments.
If it guesses wrong and you manually set `zotero_main_attachment_key`
in the md, `kb-importer` will preserve your override on the next sync
— but only if the key still refers to a current attachment. If you
edit this field, do it via `kb-write paper-zone` or directly; be
aware that deleting the attachment in Zotero will cause your override
to fall back to the heuristic.

---

## 4. Concurrency: mtime guard

Multiple agents can run against the same KB (MCP server, local CLI,
your editor). To detect conflicts, every write operation follows this
protocol:

1. `stat` the target file; record `mtime`.
2. Prepare the new content.
3. Atomic write: write to a temp file, then `os.replace` to target.
4. Before replacing, re-`stat` and compare mtime. If mtime changed
   between steps 1 and 3, **abort** with a clear error.

This catches the case where another agent/editor modified the file
while you were composing your write. `kb-write` does all this for
you if you pass `--expected-mtime`.

For `create_*` operations there's no conflict check — but the target
path must not exist (otherwise we'd silently overwrite).

### 4.1 Write lock

`kb-write` also uses a per-KB advisory lock: `.kb-mcp/write.lock`.
Only one `kb-write` process at a time holds the lock. The lock is
process-scoped and released on exit (including crash). This is a
belt-and-braces safety net; the mtime guard is the real check.

---

## 5. Atomic writes

Every write goes through `os.replace(temp_path, target_path)`, which
is atomic on POSIX and Windows (NTFS). A crash mid-write leaves either
the old file or the new file — never a half-written one.

`kb-write` writes the temp file in the same directory as the target
(not `/tmp`) to ensure `os.replace` doesn't cross filesystems.

---

## 6. git auto-commit

**Default: ON.** Every `kb-write` operation that modifies the KB
follows the write with:

```bash
git add <changed_files>
git commit -m "<operation>: <target> [kb-write]"
```

This gives you a clean rollback path. Agents that make a series of
related edits will produce a series of commits; squash them
afterward if you prefer a clean history.

Turn off per-call with `--no-git-commit`, or globally via config:

```yaml
git:
  auto_commit: false
```

If the KB isn't a git repo, the flag is silently ignored.

Commit messages follow this shape:
```
<op>: <target> [kb-write]

<optional body from --commit-message flag>
```

where `<op>` is `create_thought`, `update_topic`, `ai_zone_update`,
`add_tag`, `add_ref`, `delete_thought`, etc.

---

## 7. Triggering re-index

After a successful write, `kb-write` calls the `kb-mcp` index
command (if available) so that:

- FTS5 reflects the new text
- vector embeddings are generated for the new/changed paper
- the link graph picks up any new references

If `kb-mcp` isn't installed, the re-index step is skipped with a
warning. The next explicit `kb-mcp index` run will catch up.

---

## 8. Slug and ID conventions

- **Thought slug**: must begin with `YYYY-MM-DD-` followed by a short
  descriptive slug. Example: `2026-04-22-passivity-gfm-link`.
  If you create a thought without specifying a slug, `kb-write`
  auto-generates one from title + today's date.
- **Topic slug**: short kebab-case, no date. Example: `gfm-stability`,
  `stochastic-port-hamiltonian`. Topics can be nested
  (`attention/overview.md` is a valid hierarchy).
- **Paper key / note key**: 8-char uppercase alphanumeric, assigned
  by Zotero. Never generated locally.

---

## 9. kb_refs syntax

The `kb_refs` frontmatter list declares outgoing references. **v26**
accepted forms for each entry:

```yaml
kb_refs:
  - papers/ABCD1234                     # paper (or book / chapter)
  - papers/BOOKKEY-ch03                 # specific book chapter
  - topics/agent-created/gfm-stability  # AI topic (was topics/... in v25)
  - topics/standalone-note/NOTE001      # Zotero note (was zotero-notes/... in v25)
  - thoughts/2026-04-20-idea            # dated thought
  - ABCD1234                            # bare key — resolver guesses type
```

Prefer the explicit form when you know the type. Bare keys work
because the indexer tries paper → topic → thought → note in that
order, but may resolve wrong if keys collide.

**Deprecated (REJECTED by v26 kb-write):**

```yaml
# DO NOT USE — kb-write raises RuleViolation:
- zotero-notes/NOTE001       # → use topics/standalone-note/NOTE001
- topics/gfm-stability       # → use topics/agent-created/gfm-stability
```

You may also reference things inside the md body via:

- `[[ABCD1234]]` — wikilink, type auto-detected
- `[title](papers/ABCD1234.md)` — markdown link
- `@alice2024ph` — citation key (whole-work paper only; resolved via
  citation_key frontmatter field — chapter mds don't participate)

The link graph (Phase 2c) extracts all four forms and fuses them.

---

## 10. Violations and recovery

If you (or another agent) violated these rules, run:

```bash
kb-write doctor
```

It scans the whole KB and reports:

- Missing / duplicated AI zone markers
- `zotero_*` fields modified by non-importer
- Orphaned `kb_refs` entries
- Empty mandatory fields
- Mismatched slug conventions

`kb-write doctor --fix` will repair what's safely repairable
(re-insert missing AI zone markers, dedupe tag lists, etc.). It
never touches `zotero_*` or `fulltext_*` content — those must be
fixed by `kb-importer sync`.

---

## 11. What you CAN freely do

Just so this isn't only a list of "don'ts":

- Create any number of thoughts per day.
- Create topics as organizational headers over related papers.
- Edit your own thoughts/topics freely (full file replace).
- Add kb_tags and kb_refs to any md (including papers/notes).
- Write freely in the AI zone of a paper to record analysis,
  connections, questions.
- Link liberally — the graph resolves dangling references
  automatically when the target is later added.

---

## 11.5 Batch re-read (`kb-write re-read`)

`kb-write re-read` picks N papers via a pluggable selector and
runs `re_summarize()` on each. It's a thin batch layer over the
single-paper re-summarize command — all the write-safety rules of
re-summarize still apply (mtime guard, git commit, audit log,
atomic write, lock).

Invariants specific to re-read:

1. **One event per paper**. Every chosen paper gets exactly one
   entry in `<kb_root>/.kb-mcp/events.jsonl` (event_type=re_read)
   with category ∈ {success, skip_mtime_conflict, skip_llm_error,
   skip_pdf_missing, dryrun_selected}. `kb-mcp report` aggregates
   these.
2. **Failure isolation**. If paper #3 of a batch of 10 fails, the
   remaining 7 still run. The batch exits with non-zero exit code
   if any paper skipped (so cron/CI can alarm), but doesn't halt
   mid-batch.
3. **Selector is declarative**. Which papers get picked depends on
   `--selector`; same selector + same `--seed` always picks the
   same papers (reproducibility for debugging).
4. **Source defines what's eligible**. `--source papers` (default)
   means "anything under papers/*.md". `--source storage` narrows
   to papers whose PDF exists on disk.
5. **Two dry-runs exist, don't confuse them**:
   - `--dry-run-select` (re-read flag): pick papers, log DRYRUN
     events, don't call LLM at all. Cheap preview.
   - `--dry-run` (global write flag): runs LLM, computes new
     summary, but doesn't splice to disk. Useful for verifying
     LLM output without committing.

Agents using re-read (e.g. a nightly maintenance agent):

- Prefer `unread-first` (default) for recurring runs — it shrinks
  the "never re-read" backlog before repeating papers.
- Use `related-to-recent` when the user has been active on a
  specific topic this week — it surfaces their references.
- Never second-guess selector choice via heuristics in prompts;
  pick a selector explicitly and note WHY in the commit message
  that re-read will generate.

---

## 12. Persistent user preferences (`.agent-prefs/`)

The user keeps persistent quirks, style rules, and research context
in `.agent-prefs/` at the KB root. These are **not** thoughts or
topics — they're meta-instructions about how you (the agent) should
behave across sessions.

**Required behavior**:

1. At the start of every substantive session, read every `.md` file
   under `.agent-prefs/`. (The `.` prefix means kb-importer and
   kb-mcp indexers skip this directory — only agents reading the
   filesystem directly see them.)
2. Apply prefs silently. Don't narrate "I see you prefer X"; just
   do X.
3. When the user says "remember to...", "from now on...", "I
   prefer..." — propose saving to an appropriate pref file via
   `kb-write pref add` or `kb-write pref update`.
4. Never modify `.agent-prefs/` files outside of `kb-write pref`
   (same rule as any other write: go through the CLI for validation
   + git commit).

**Precedence** (when multiple sources of behavior conflict):

1. In-conversation direct user instructions (highest).
2. Scope-specific pref (e.g. `.agent-prefs/ai-summary.md` during a
   summary task) beats `global.md`.
3. Within the same scope, higher `priority` frontmatter wins.
4. Within same scope + priority, newer `last_updated` wins.
5. `AGENT-WRITE-RULES.md` rules override prefs on write safety
   (prefs can't weaken rules).

See `.agent-prefs/README.md` for the file-format convention and
a starter set of recommended files.

---

## 13. Agent discovery checklist

When an agent (Claude Code, opencode, Cursor, MCP-based Claude,
custom script) first enters this repo, it should find its entry
point via one of:

| Agent                | Default entry file    |
|----------------------|------------------------|
| Claude Code          | `CLAUDE.md`            |
| opencode             | `AGENTS.md`            |
| Cursor               | `.cursorrules`         |
| Aider                | `.aiderrc` (via `--read`) |
| anyone else          | `README.md`            |

All five entry files in this repo point to THIS document
(`AGENT-WRITE-RULES.md`) and to `.agent-prefs/`. If you maintain
this KB and add support for a new agent tool that uses a different
default file, create a copy/symlink with the same redirect content.

The five entry files are managed by `kb-write init`. Running
`kb-write init` on an existing KB is safe — it only creates missing
files, never overwrites.

---

## Appendix A: field glossary

For the complete list of reserved frontmatter fields, see
`kb_importer/src/kb_importer/md_builder.py` (function
`_build_paper_frontmatter`).

## Appendix B: related docs

- `kb_importer/README.md` — how papers arrive
- `kb_mcp/README.md` — how to query the KB
- `kb_write/README.md` — this writing layer (high-level)
