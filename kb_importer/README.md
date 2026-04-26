# kb-importer

CLI that translates a Zotero library into a KB repository of markdown
files. One paper per md, with metadata, abstract, and Zotero notes.
Child notes are embedded in the paper's md; standalone notes get
their own md in `topics/standalone-note/`.

This is the Zotero-side tool. It has no knowledge of RAG, vectors, or
MCP. Those live in the `kb-mcp` project and consume the md files this
tool produces.

## Two source modes

kb-importer reads Zotero metadata in one of two modes:

| mode | metadata source | PDFs | Zotero running? | network? |
|------|----------------|------|-----------------|---------|
| `web` (default since 0.28.0) | api.zotero.org (cloud) | local `zotero_storage_dir` | no | yes |
| `live` | localhost:23119 local API | local `zotero_storage_dir` | yes | no |

Both modes use the **same local storage directory** for PDFs, because
Zotero item keys are the same across all APIs. So:

- **Running on a server without Zotero** (or anywhere headless) →
  `web` (the default). Works via the cloud API; rsync
  `~/Zotero/storage/` to the machine once so PDFs are available
  locally.
- **Running on your main machine with Zotero open** → you can
  pick either. `live` avoids the API key + network. `web` still
  works.

> **TODO**: a future "sqlite" mode could read `zotero.sqlite` directly
> for a fully offline snapshot. Deferred — the Zotero SQLite schema is
> not a stable API.

## Install

```bash
cd kb_importer
pip install -e .
```

Requires Python 3.10+. Pins `kb-core==<same-version>` and
`kb-write==<same-version>` — install those first if you're coming
from a fresh venv (see repo-root DEVELOPMENT.md / DEPLOYMENT.md).

## Setup

**Config storage policy.** All kb-importer config lives at
`<workspace-parent>/.ee-kb-tools/config/kb-importer.yaml`. Nothing
is read from `~/.config/`, `~/.local/share/`, `/etc/`, or any other
system path. API keys come from env variables only — never stored
in config files.

### Canonical workspace layout

```
<workspace-parent>/
├── .ee-kb-tools/
│   └── config/
│       └── kb-importer.yaml    (created by `kb-write init`)
├── ee-kb/                      (your KB — paper mds, notes, topics, thoughts)
└── zotero/
    └── storage/                (Zotero attachment store — rsynced for web mode)
```

`kb-write init` run from inside `ee-kb/` scaffolds this layout
(0.29.8+: auto-creates `.ee-kb-tools/config/` too).

### Option A: web mode (default, recommended)

1. **Get your Zotero userID and API key**:
   - Go to <https://www.zotero.org/settings/keys>
   - Your userID is shown as *"Your userID for use in API calls is: 1234567"*
   - Click "Create new private key", give it a name, set **read-only**
     access to your library, save the generated key (long hex string).

2. **Rsync storage from a machine with Zotero to this machine**
   (one-time; repeat whenever you add new PDFs):

   ```bash
   # On the Zotero machine:
   rsync -av --delete ~/Zotero/storage/ user@target:/path/to/workspace/zotero/storage/
   ```

3. **Edit** `<workspace-parent>/.ee-kb-tools/config/kb-importer.yaml`
   (the file exists after `kb-write init`; fill in your library_id):

   ```yaml
   zotero:
     library_id: "YOUR_USER_ID"    # as a string; e.g. "1234567"
     library_type: user            # "user" or "group"
     source_mode: web              # (the default as of 0.28.0)
     api_key_env: ZOTERO_API_KEY   # env var name; value set in your shell rc
   ```

4. **Export the API key** in your shell rc (`~/.bashrc` / `~/.zshrc`):

   ```bash
   export ZOTERO_API_KEY=<your-api-key>
   ```

   Or put it in a file that's sourced by your shell / systemd unit /
   cron job. Never commit the key itself to git.

### Option B: live mode

1. **Start Zotero** and enable the local API:
   Settings → Advanced → "Allow other applications on this computer
   to communicate with Zotero".

2. **Set `source_mode: live`** in `kb-importer.yaml`:

   ```yaml
   zotero:
     source_mode: live
   ```

   No `library_id` or API key needed in live mode — Zotero's local
   API authenticates by virtue of running on the same machine.

3. The rest is the same. `zotero_storage_dir` defaults to
   `<workspace-parent>/zotero/storage/` if that directory exists
   (canonical layout); otherwise set it explicitly via
   `KB_ZOTERO_STORAGE` or `--zotero-storage`.

### CLI overrides (for either mode)

All config values can be overridden by CLI flags or env vars:

```bash
# Flags
kb-importer --zotero-source web \
            --zotero-library-id YOUR_USER_ID \
            --zotero-storage ~/zotero-storage \
            --kb-root ~/ee-kb \
            status

# Env vars
export KB_ZOTERO_SOURCE=web
export ZOTERO_LIBRARY_ID=YOUR_USER_ID
export ZOTERO_API_KEY=xxx
export KB_ZOTERO_STORAGE=~/zotero-storage
export KB_ROOT=~/ee-kb
kb-importer status
```

Precedence: CLI flags > env vars > config file > defaults.

## Usage

```bash
# See progress.
kb-importer status

# List pending papers filtered by a collection.
kb-importer list papers --collection "Deep Learning"

# Import a specific batch.
kb-importer import papers ABCD1234 EFGH5678

# Import everything in a Zotero collection.
kb-importer import papers --collection "Deep Learning"

# Import all pending standalone notes.
kb-importer import notes --all-pending

# Sync: re-import items whose Zotero state has changed.
kb-importer sync

# Find md files whose Zotero items are gone.
kb-importer check-orphans

# Preview mode (no writes).
kb-importer --dry-run import papers --all-pending
```

### Automated fulltext pipeline (`--fulltext`)

Since v18+, kb-importer can generate AI summaries itself by extracting
PDF text and calling an LLM, instead of the manual
show-template / set-summary dance described below. Two sub-pipelines
pick automatically based on Zotero `item_type`:

**Short pipeline** — journalArticle, conferencePaper, preprint.
Reads the PDF, passes the truncated fulltext + metadata to the LLM
with a 7-section prompt, writes the JSON result into the `## AI
Summary (from Full Text)` region of the paper md (between
`<!-- kb-fulltext-start -->` / `<!-- kb-fulltext-end -->` markers).

**Long pipeline** — book, bookSection, report, thesis.
Splits the PDF into chapters (PDF bookmarks → regex → LLM-assisted
fallback), then calls the LLM once per chapter, each producing a
thought md under `thoughts/<date>-<key>-ch<NN>-<slug>.md`. The
parent paper md's fulltext region becomes a chapter-index table
pointing at those thought files.

```bash
# Metadata import + fulltext in one run (default provider: Gemini)
kb-importer import papers --all-pending --fulltext --all-unprocessed -y

# Backfill fulltext on already-imported papers without a summary
kb-importer import papers --fulltext --all-unprocessed -y

# Force re-process (overwrites existing summary)
kb-importer import papers --force-fulltext --only-key ABCD1234 -y

# Switch provider / model
kb-importer import papers ... --fulltext \
    --fulltext-provider openai \
    --fulltext-model gpt-4o-mini

# Use OpenRouter to pick any catalog model (openai / anthropic /
# google / deepseek / …) behind one API key.
#   export OPENROUTER_API_KEY=sk-or-...
kb-importer import papers ... --fulltext \
    --fulltext-provider openrouter \
    --fulltext-model anthropic/claude-sonnet-4.5

# Inspect chapter detection for a long paper without LLM spend
kb-importer import papers --fulltext --longform-dryrun --only-key BOOKKEY1
```

**Supported providers + defaults**:

| `--fulltext-provider` | Default model | API key env var | Notes |
|-----------------------|---------------|-----------------|-------|
| `gemini` (default) | `gemini-3.1-pro-preview` | `GEMINI_API_KEY` | Free tier ~1000 papers/day; daily-quota fallback on by default |
| `openai` | `gpt-4o-mini` | `OPENAI_API_KEY` | |
| `deepseek` | `deepseek/deepseek-v4-pro` | `DEEPSEEK_API_KEY` | Cheapest among direct-provider options |
| `openrouter` | `openai/gpt-oss-120b:free` | `OPENROUTER_API_KEY` | Free-tier open-weight model (120B MoE, ~5B active). Capability meaningfully below paid GPT-4-class models; for important libraries override with `--fulltext-model google/gemini-2.5-flash` / `anthropic/claude-sonnet-4.5` / `openai/gpt-4o` etc. |

**Note on RAG vs fulltext config**: this `--fulltext-provider`
setting is completely independent from kb-mcp's RAG embedding
provider. The embedding pipeline has its own `embeddings.provider`
in `kb-mcp.yaml` with its own supported providers (openai / gemini /
openrouter) and its own env vars. OpenRouter specifically uses
`OPENROUTER_API_KEY` here and `OPENROUTER_EMBEDDING_API_KEY` on the
embedding side — different keys for different pipelines by default,
though a single `OPENROUTER_API_KEY` works for both via embedding-
side fallback.

**Gemini daily-quota fallback** (default on):

Gemini 3.1-pro-preview has an RPD=250 daily quota. If you hit it
mid-run, kb-importer silently switches the rest of the session to
`--fulltext-fallback-model` (default `gemini-2.5-pro`, RPD several
thousand) and continues. Disable with `--no-fulltext-fallback`.

```bash
# Pick the fallback model explicitly
kb-importer import papers --fulltext \
    --fulltext-fallback-model gemini-2.5-flash   # cheaper/faster fallback

# No fallback — hit quota, stop the run
kb-importer import papers --fulltext --no-fulltext-fallback
```

**Git auto-commit** (default on):

Each pipeline stage commits its writes to git:

- metadata import → **one commit per run** (batch)
- fulltext writeback (short pipeline) → **one commit per paper**
- longform ingest → **one commit per book** (parent md + chapter thoughts)

Opt out with `--no-git-commit`. Safe no-op if `kb_root` isn't a git
repo or if `kb_write` isn't installed.

**Longform idempotency**:

Re-running `--fulltext` on a paper that already produced chapter
thoughts will skip the LLM calls (detected via existing
`thoughts/*-<key>-ch*.md` files). To truly regenerate a book, first
delete the existing chapter thoughts.

### Manual AI summary workflow (legacy)

Still supported for cases where you prefer to drive the LLM
yourself (e.g. from Claude Desktop with the PDF attached, or from a
chat thread where you've already discussed the paper):

```bash
# 1. Migrate any existing "AI Summary" child notes you've already
#    written in Zotero into the paper mds.
kb-importer import-summaries

# 2. See which imported papers still lack a summary.
kb-importer list papers --imported --no-summary

# 3. For each paper without a summary, drive an LLM agent yourself:
#    a) Get the prompt template:
kb-importer show-template > /tmp/prompt.md

#    b) Have an LLM read the PDF + follow the template, producing a
#       summary text. (kb-importer's manual mode deliberately stays
#       out of the LLM business.)

#    c) Feed the generated summary back:
kb-importer set-summary ABCD1234 < /tmp/summary.md
```

Book, bookSection, report, thesis, and webpage item types are never
eligible for summaries — the template is paper-shaped. `set-summary`
will refuse them and `import-summaries` will skip them silently.

You can edit the bundled template to change structure, language, or
style:

```bash
kb-importer show-template --path   # prints the file path
# edit that file; kb-importer reads from it every time.
```

Switching modes at runtime is fine — both modes produce identical md
output (same Zotero keys, same data structure). You can e.g. do the
initial import on your main machine in `live` mode, then schedule
periodic syncs on the server in `web` mode.

## How "done" is tracked

Progress lives in the filesystem:

- **Papers**: a paper is "imported" iff `papers/{paper_key}.md` exists
  in the KB repo.
- **Standalone notes**: imported iff `topics/standalone-note/{key}.md`
  exists.

There is no separate state file. 0.29.1 also removed the
`storage/_archived/` dance that existed pre-0.29 — attachments stay
flat under `storage/<attachment_key>/` and the md is the authority
on import state.

## Sync: what triggers a re-import

In Zotero every item has its own version number, and editing a
**child note** bumps the note's version but **not** the parent paper's
version. So `sync` checks three independent signals per paper:

1. Paper's own `zotero_version` bumped.
2. Max version across child notes (`zotero_max_child_version`) bumped.
3. Child note count changed (deletion doesn't bump any remaining
   item's version, so we need this separately).

Any one of these changing triggers re-generation of the md, preserving
`kb_*` frontmatter and the `<!-- kb-ai-zone -->` region.

Standalone notes are simpler: just compare their own `zotero_version`.

## What's preserved on re-import

When a paper is re-imported (directly or via `sync`), kb-importer
preserves:

- Every frontmatter field starting with `kb_`.
- Content between `<!-- kb-ai-zone-start -->` and `<!-- kb-ai-zone-end -->`.
- Content between `<!-- kb-fulltext-start -->` and
  `<!-- kb-fulltext-end -->` (the AI summary section). v22+ splices
  updates in-place via the `inject_fulltext` surgical rewriter rather
  than regenerating the whole file, so the AI-summary region is safe
  across both metadata re-imports and `--force-fulltext` reruns.

Everything else (`zotero_*` fields, core fields, Zotero notes section,
attachments section, body) is regenerated from Zotero.

## Rate limiting (web mode)

Zotero's cloud API has rate limits. pyzotero handles `429` responses
and `Backoff:` headers automatically, but large batches (hundreds of
papers at once) may still slow down. If you see the importer stall, it
is likely being rate-limited — let it ride.

## Notes for LLM agents driving this CLI

If you're an LLM agent orchestrating kb-importer (e.g. to batch-summarize
papers), read this section first. Observed gotchas:

### Paper keys vs attachment keys

The two kinds of Zotero keys **look identical** (8-character
alphanumeric) but identify different things:

- **Paper key** (`ABCD1234`): a top-level bibliographic item.
  `papers/{paper_key}.md` is named by this.
- **Attachment key** (`XY7ZK3A2`): one PDF attached to a paper. This
  is what names `~/Zotero/storage/{attachment_key}/` subdirectories.

A paper can have multiple attachments. Commands like `set-summary`
take **paper keys**, never attachment keys. Passing an attachment
key where a paper key is expected gets a friendly error pointing
you to the right paper key (as of v0.1.0), but save yourself the
round-trip: always start from paper keys.

**How to get paper keys cheaply**: `kb-importer list papers --imported`
(no `--with-titles`) prints raw paper keys with zero Zotero API calls.
For a specific subset: `list papers --imported --no-summary` shows
papers eligible for `set-summary`.

### Connectivity check

Use `kb-importer status --quick` — not plain `status`. The full
`status` does a paginated full-library scan (minutes on large
libraries in web mode). `--quick` does one lightweight API call.

### Listing papers for batch processing

`list papers --limit N` is now efficient: it stops enumerating after
N matches. But be aware:

- Without any of `--year`/`--collection`/`--tag`/`--with-titles`,
  it skips Zotero fetches entirely and prints keys only (fast).
- With any filter above, it fetches from Zotero one at a time and
  stops at N matches. Plan accordingly: if you want titles for 5
  papers, `--limit 5 --with-titles` does 5 API calls, not 1000+.

### Concurrent runs

`kb-importer import` does not participate in the kb-write write-lock
(see `kb_write/AGENT-WRITE-RULES.md`). It writes md files directly
via `atomic_write`, treating Zotero as the source of truth. Practical
implications:

- **Two `kb-importer import` runs in parallel on the same KB**: safe
  for correctness (both write the same Zotero-derived content), but
  wasteful. Serialise them.
- **`kb-importer import` + `kb-write <anything>` concurrently**: the
  kb-write side may hit `WriteConflictError` if it's editing a paper
  md kb-importer is simultaneously overwriting. Just rerun the
  kb-write command after import finishes.
- **Cron setup**: run `kb-importer import` during a quiet window
  (e.g. 3 AM) so interactive kb-write calls don't collide. If that's
  not possible, wrap the cron command with `flock` against a file
  under `~/.cache/` to make imports self-serialise.

### Batch summarization pattern

For N-paper parallel summarization: sanity-check with one paper first,
then fan out. Each agent should receive the paper key (not attachment
key), and should pipe summary text to `kb-importer set-summary KEY`.
`set-summary` returns exit code:
- `0` on success
- `2` on no-md / read error (with attachment-key hint if applicable)
- `3` on already-processed (use `--force` to overwrite)
- `4` on ineligible item type (book, thesis, report, webpage, bookSection)


