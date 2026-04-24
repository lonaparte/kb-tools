"""`kb-importer import papers|notes` — core import flow.

Flow per paper:
  1. Fetch paper + attachments from Zotero in one round-trip
     (raises ZoteroChildrenFetchError on API failure; caller SKIPS
     this paper without rewriting its md).
  2. Locate each attachment's PDF on disk under storage/.
  3. Extract preserved content from any existing md
     (AI zone + any marker-delimited regions the user wants to keep).
  4. Build new md text listing ALL attachments.
  5. Atomically write md.
  6. Optional fulltext pass runs afterwards when `--fulltext`
     (or implicit via `--all-unprocessed`) is set — calls the LLM
     per paper, writes summary into the ai-summary region, handles
     quota fallback and permanent (400/404) BadRequestError cases.

Flow per note: no attachments, nothing else.

0.29.1: the auto-archive step after step 5 is gone; attachments
stay flat under storage/. See state.py for full rationale.
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..zotero_reader import ZoteroReader
from ._shared import _positive_int

log = logging.getLogger(__name__)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("import", help="Import items from Zotero into KB.")
    p.add_argument(
        "target",
        choices=["papers", "notes"],
        help="What to import.",
    )
    p.add_argument(
        "keys",
        nargs="*",
        help="Specific Zotero item keys to import. If omitted, use filters.",
    )
    p.add_argument("--collection",
                   help="Import all pending items in this collection.")
    p.add_argument("--tag", help="Import all pending items with this tag.")
    p.add_argument("--year", type=int,
                   help="Import all pending items of this year.")
    p.add_argument("--all-pending", action="store_true",
                   help="Import every pending item (not yet imported).")
    p.add_argument(
        "--all-unprocessed", action="store_true",
        help="Include already-imported papers that still have "
             "fulltext_processed != true. Without this flag, a run "
             "is always scoped to pending papers; with it, the run "
             "also backfills fulltext for papers imported in earlier "
             "runs. Implies --fulltext. Only meaningful for "
             "target=papers.",
    )
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip confirmation prompt.")

    # Fulltext pass: AFTER metadata import, extract PDF fulltext
    # (Zotero API first, local PDF fallback), generate a 7-section
    # AI summary, and write it into each paper's md body. Only
    # applies to --target papers. Separate pass so the metadata
    # import stays fast and the fulltext step is idempotent and
    # resumable.
    p.add_argument(
        "--fulltext", action="store_true",
        help="After metadata import, extract PDF fulltext and "
             "generate AI summary for each paper. Only applies "
             "with target=papers. Idempotent: skips papers where "
             "fulltext_processed=true unless --force-fulltext. "
             "Routes books/theses to the long-form pipeline "
             "automatically; override with --longform / --no-longform.",
    )
    p.add_argument(
        "--force-fulltext", action="store_true",
        help="Reprocess papers even if already summarised. "
             "Replaces the fulltext region (between the "
             "<!-- kb-fulltext-start/end --> markers) with a fresh "
             "summary; leaves AI zone, attachments, and any other "
             "content outside those markers untouched. Implies "
             "--fulltext.",
    )
    p.add_argument(
        "--longform", dest="longform_override",
        action="store_const", const="long",
        help="Force every selected paper through the long-form "
             "chapter pipeline, regardless of item_type. Diagnostic / "
             "pilot use — e.g. to pilot chapter splitting on a "
             "journalArticle-style book-length preprint.",
    )
    p.add_argument(
        "--no-longform", dest="longform_override",
        action="store_const", const="short",
        help="Force every selected paper through the short 7-section "
             "pipeline, even books/theses. NOT recommended — will "
             "produce low-quality results for long documents. Use "
             "only if you understand the tradeoff.",
    )
    p.add_argument(
        "--longform-dryrun", action="store_true",
        help="Run only stage 1 of the long-form pipeline (chapter "
             "splitting) and print the detected chapter table. Does "
             "NOT call the LLM or write any thoughts. Useful for "
             "sanity-checking that chapters were correctly detected "
             "before committing API spend.",
    )
    p.add_argument(
        "--fulltext-provider", default="gemini",
        choices=["gemini", "openai", "deepseek", "openrouter"],
        help="LLM provider for summary generation. Default gemini "
             "(free tier covers ~1000 papers/day). openrouter "
             "routes to many upstream models via OPENROUTER_API_KEY; "
             "see --fulltext-model examples.",
    )
    p.add_argument(
        "--fulltext-model", default=None,
        help="Override default model for the chosen provider. "
             "Defaults: gemini→gemini-3.1-pro-preview, "
             "openai→gpt-4o-mini, deepseek→deepseek-chat, "
             "openrouter→openai/gpt-oss-120b:free (free-tier "
             "open-weight; capability may lag paid models — override "
             "for important libraries). "
             "For cheaper gemini runs, try gemini-3-flash-preview "
             "or gemini-3.1-flash-lite. OpenRouter upgrade examples: "
             "google/gemini-2.5-flash (cheap paid), "
             "anthropic/claude-sonnet-4.5 (high quality), "
             "openai/gpt-4o (OpenAI flagship).",
    )
    p.add_argument(
        "--fulltext-fallback-model", default="gemini-2.5-pro",
        help="Gemini only: model to switch to when the primary model "
             "hits its daily quota (HTTP 429 with per-day kind). "
             "Default gemini-2.5-pro has a much larger RPD allowance "
             "than 3.1-pro-preview (250), so a full-library run of "
             "~1000 papers can finish in one pass even after the "
             "primary is exhausted. The switch is session-sticky: "
             "once triggered, all remaining papers in this run use "
             "the fallback. Set to empty string together with "
             "--no-fulltext-fallback to disable.",
    )
    p.add_argument(
        "--no-fulltext-fallback", action="store_true",
        help="Disable the automatic fallback described in "
             "--fulltext-fallback-model. With this flag, a daily "
             "quota exhaustion raises and stops the fulltext pass "
             "(the way v22 and earlier behaved).",
    )
    p.add_argument(
        "--fulltext-max-tokens", type=_positive_int, default=8000,
        help="Max output tokens per summary request (default 8000). "
             "Gemini 2.5/3.x models consume part of this as thinking "
             "tokens before emitting JSON; under-sizing it causes "
             "truncated responses and 'LLM returned non-JSON twice' "
             "errors. Bump higher for verbose papers.",
    )
    p.add_argument(
        "--only-key", default=None,
        help="Comma-separated Zotero keys to restrict the whole run. "
             "Equivalent to passing the keys as positional arguments "
             "(but mirrors `kb-mcp index --only-key`'s shape). Both "
             "metadata import AND fulltext pass honour this filter. "
             "Useful for pilot runs before a full-library pass.",
    )
    p.add_argument(
        "--no-git-commit", action="store_true",
        help="Disable auto-commit of kb-importer's writes. By default, "
             "kb-importer commits:\n"
             "  - one commit per `import papers` run for metadata "
             "writes (build_paper_md output)\n"
             "  - one commit per paper for fulltext summary writes\n"
             "  - one commit per book for longform chapter-thought "
             "batches\n"
             "matching kb-write's auto-commit behaviour so ee-kb stays "
             "consistently versioned regardless of which tool wrote "
             "the change. Pass --no-git-commit to skip all three; the "
             "md files land in the working tree un-staged so a human "
             "can review and commit manually.",
    )

    p.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    # Cross-process advisory lock — prevents two concurrent
    # kb-importer import runs on the same KB from racing on md
    # writes and doubling up Zotero API traffic. The lock is held
    # for the whole duration of the import (hours, for large
    # fulltext batches) and auto-released on process exit.
    #
    # --dry-run skips the lock: it does no writes, so concurrent
    # dry-runs are safe and blocking them would surprise users
    # poking at the CLI while a real run is in progress.
    from ..import_lock import import_lock, ImportLockHeld

    if getattr(args, "dry_run", False):
        return _run_locked(args, cfg)
    try:
        with import_lock(cfg.kb_root):
            return _run_locked(args, cfg)
    except ImportLockHeld as e:
        print(f"Error: {e}", file=sys.stderr)
        return 3


def _run_locked(args: argparse.Namespace, cfg: Config) -> int:
    """Actual import body. Extracted so the lock wrapper in `run`
    can be tested / bypassed independently."""
    # Connect.
    try:
        reader = ZoteroReader(cfg)
    except Exception as e:
        # Error phrasing depends on which source mode was configured,
        # since the likely causes differ (local: daemon not running;
        # web: API key / library_id / network).
        mode = getattr(cfg, "zotero_source_mode", None) or "?"
        if mode == "web":
            print(
                f"Error: could not initialise Zotero web source: {e}\n"
                f"  Check: ZOTERO_API_KEY env var, "
                f"zotero.library_id in config, network connectivity."
            )
        elif mode == "live":
            print(
                f"Error: could not connect to Zotero local API: {e}\n"
                f"  Check: is the Zotero desktop app running? "
                f"Preferences → Advanced → 'Allow other applications "
                f"on this computer to communicate with Zotero' must "
                f"be enabled."
            )
        else:
            print(f"Error: could not initialise Zotero source "
                  f"(mode={mode!r}): {e}")
        return 2

    # Build target key set.
    if args.target == "papers":
        keys = _resolve_paper_keys(args, cfg, reader)
        if not keys:
            print("No matching pending papers.")
            return 0
    else:
        keys = _resolve_note_keys(args, cfg, reader)
        if not keys:
            print("No matching pending notes.")
            return 0

    # Confirmation (unless -y, --dry-run also skips prompt since it doesn't
    # modify anything).
    if not args.yes and not getattr(args, "dry_run", False):
        resp = input(
            f"About to process {len(keys)} {args.target}. Continue? [y/N] "
        ).strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Process each item.
    success = 0
    failed = 0
    dry_run = getattr(args, "dry_run", False)
    written_keys: list[str] = []  # for batch git commit at end

    from ..zotero_reader import ZoteroChildrenFetchError
    for key in sorted(keys):
        try:
            if args.target == "papers":
                _process_paper(cfg, reader, key, dry_run=dry_run)
            else:
                _process_note(cfg, reader, key, dry_run=dry_run)
            success += 1
            if not dry_run:
                print(f"✓ {key}")
                written_keys.append(key)
            else:
                print(f"(dry-run) would import {key}")
        except ZoteroChildrenFetchError as e:
            # v0.29.0: children fetch failed. We deliberately do NOT
            # rewrite the md in this case — pre-0.29 the bug was that
            # the fetch error was swallowed and the md got rewritten
            # with `attachment_keys: []` / `max_child_version: 0`,
            # causing the attachment-thrash documented in the
            # CHANGELOG. Skip this paper, log, move on.
            failed += 1
            print(
                f"⚠ {key}  SKIPPED: could not fetch children from "
                f"Zotero ({e}). The md was left UNCHANGED. Re-run "
                f"when the Zotero API is healthy.",
                file=sys.stderr,
            )
        except Exception as e:
            failed += 1
            log.exception("import failed for %s", key)
            print(f"✗ {key}  {type(e).__name__}: {e}", file=sys.stderr)

    # Batch commit for metadata pass. One commit per `import` run
    # (not per paper) — keeps log compact even for 1000-paper runs
    # while still giving a clean atomic checkpoint. Fulltext and
    # longform passes below commit per-paper/per-book because their
    # writes are heavy and each is a meaningful atomic unit.
    _auto_commit_metadata_batch(cfg, args, written_keys)

    total = success + failed
    # If we're going to do a fulltext pass, label this as the metadata
    # phase so the user doesn't see "2/2 succeeded" and think the whole
    # run is done. Previously the "Done:" line read as a final summary
    # and misled users when the fulltext pass crashed after it.
    # --all-unprocessed implies --fulltext (otherwise there'd be
    # nothing to backfill).
    wants_fulltext = args.target == "papers" and (
        args.fulltext or args.force_fulltext or args.all_unprocessed
    )
    if wants_fulltext and not dry_run:
        print(f"Metadata import: {success}/{total} succeeded, "
              f"{failed} failed.")
    else:
        print(f"Done: {success}/{total} succeeded, {failed} failed.")

    # ------------------------------------------------------------------
    # Optional fulltext pass. Only for papers. Runs AFTER metadata is
    # on disk, so md files exist to write back into. We process each
    # paper independently — one failure doesn't abort the rest.
    # ------------------------------------------------------------------
    fulltext_rc = 0
    if wants_fulltext and not dry_run and failed < total:
        fulltext_rc = _run_fulltext_pass(
            args, cfg, reader,
            candidate_keys=set(keys),
        )

    # Exit code combines both phases. Either one failing means
    # non-zero exit so scripts / CI can detect partial failure.
    rc = 0
    if failed != 0:
        rc = 1
    elif fulltext_rc != 0:
        rc = fulltext_rc

    # v26.x: emit a single IMPORT_RUN summary event. One event per
    # command invocation (NOT one per paper) — events.jsonl is a
    # landmark log for "I ran a big operation", not a blow-by-blow
    # trace. Per-paper fulltext failures are already captured
    # individually as EVENT_FULLTEXT_SKIP entries earlier in this run;
    # this summary aggregates counts so `kb-mcp report` can show
    # "this month: 4 import runs, 62 papers added".
    from ..events import (
        record_event, EVENT_IMPORT_RUN,
        IMPORT_RUN_OK, IMPORT_RUN_PARTIAL, IMPORT_RUN_ABORTED,
    )
    total = success + failed
    if rc == 0:
        category = IMPORT_RUN_OK
    elif failed == total and total > 0:
        category = IMPORT_RUN_ABORTED
    else:
        category = IMPORT_RUN_PARTIAL
    detail = (
        f"target={args.target} "
        f"metadata={success}/{total}"
        + (f" fulltext_rc={fulltext_rc}" if wants_fulltext else "")
    )
    try:
        record_event(
            cfg.kb_root,
            event_type=EVENT_IMPORT_RUN,
            category=category,
            detail=detail,
            pipeline="import",
            extra={
                "target": args.target,
                "metadata_success": success,
                "metadata_failed": failed,
                "wants_fulltext": bool(wants_fulltext),
                "fulltext_rc": fulltext_rc,
                "dry_run": bool(dry_run),
                # Filter context: useful to grep "which run came from
                # --all-pending vs --collection Core" later.
                "filter": {
                    "all_pending":      getattr(args, "all_pending", False),
                    "all_unprocessed":  getattr(args, "all_unprocessed", False),
                    "collection":       getattr(args, "collection", None),
                    "tag":              getattr(args, "tag", None),
                    "year":             getattr(args, "year", None),
                    "keys":             len(getattr(args, "keys", None) or []),
                },
            },
        )
    except Exception:
        # Event logging is best-effort — the import result is what
        # matters; never break the CLI return code on a log write
        # failure.
        pass

    return rc


# ----------------------------------------------------------------------
# Per-concern helpers moved to sibling modules in v0.28.0.
# Import them back here so external callers that do
# `from kb_importer.commands.import_cmd import _process_paper` keep
# working. run() / _run_locked() dispatch uses these names directly.
# ----------------------------------------------------------------------
from .import_keys import (
    _check_keys_not_attachments,
    _resolve_paper_keys,
    _resolve_note_keys,
)
from .import_pipeline import (
    _process_paper,
    _process_note,
    _git_commit_enabled,
    _auto_commit_metadata_batch,
    _auto_commit_single_paper,
)
from .import_fulltext import (
    _run_fulltext_pass,
    _peek_item_type,
)
