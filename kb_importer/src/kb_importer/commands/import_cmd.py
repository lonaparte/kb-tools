"""`kb-importer import papers|notes` — core import flow.

Flow per paper:
  1. Fetch paper + attachments from Zotero in one round-trip.
  2. Locate each attachment's PDF on disk (under storage/ or _archived/).
  3. Extract preserved content from any existing md.
  4. Build new md text listing ALL attachments.
  5. Atomically write md.
  6. On success, archive all unarchived attachment dirs in bulk.
  7. Any failure past step 5 is logged but doesn't erase the md —
     future runs can re-archive.

Flow per note: no attachments, no archive step.
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..md_builder import (
    build_note_md,
    build_paper_md,
    note_md_path,
    paper_md_path,
)
from ..md_io import atomic_write, extract_preserved
from ..state import (
    archive_attachments,
    find_pdf,
    imported_note_keys,
    paper_is_imported,
    scan_attachments,
)
from ..zotero_reader import ZoteroItem, ZoteroReader

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
        choices=["gemini", "openai", "deepseek"],
        help="LLM provider for summary generation. Default gemini "
             "(free tier covers ~1000 papers/day).",
    )
    p.add_argument(
        "--fulltext-model", default=None,
        help="Override default model for the chosen provider. "
             "Defaults: gemini→gemini-3.1-pro-preview, "
             "openai→gpt-4o-mini, deepseek→deepseek-chat. "
             "For cheaper gemini runs, try gemini-3-flash-preview "
             "or gemini-3.1-flash-lite.",
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
        "--fulltext-max-tokens", type=int, default=8000,
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
# Key resolution
# ----------------------------------------------------------------------

def _check_keys_not_attachments(cfg: Config, user_keys: set[str]) -> set[str]:
    """Warn on (and filter out) user-supplied keys that are actually
    attachment keys of already-imported papers.

    This catches the common mistake of passing a `storage/` subdir
    name (attachment key) where a paper key is expected. We scan
    already-imported paper mds' `zotero_attachment_keys` frontmatter
    to build a reverse map, then check each user-supplied key.

    Keys recognized as attachments are skipped with a warning pointing
    to the parent paper. Unknown keys pass through silently — they
    might be paper keys we haven't imported yet.

    Only runs when the user passes explicit keys (not --all-pending),
    so it's O(N mds) but bounded.
    """
    # Local imports to keep the module-level imports clean.
    from ..md_io import read_md
    from ..md_builder import paper_md_path

    imported = {
        p.stem
        for p in cfg.papers_dir.glob("*.md")
        if not p.name.startswith(".")
    } if cfg.papers_dir.exists() else set()

    att_to_paper: dict[str, str] = {}
    for pk in imported:
        try:
            post = read_md(paper_md_path(cfg.kb_root, pk))
        except Exception:
            continue
        atts = post.metadata.get("zotero_attachment_keys", []) or []
        for ak in atts:
            if isinstance(ak, str):
                att_to_paper[ak] = pk

    safe_keys: set[str] = set()
    for key in user_keys:
        parent = att_to_paper.get(key)
        if parent and parent != key:
            print(
                f"⚠  {key!r} looks like an attachment key "
                f"(belongs to paper {parent!r}). Skipping. "
                f"If you want to re-import that paper, use {parent!r}.",
                file=sys.stderr,
            )
            continue
        safe_keys.add(key)
    return safe_keys


def _resolve_paper_keys(
    args: argparse.Namespace, cfg: Config, reader: ZoteroReader
) -> set[str]:
    # --only-key is a comma-separated form of positional keys; merge
    # them so both drive paper selection. Previously --only-key only
    # affected the fulltext pass, which was surprising given its name.
    user_keys: list[str] = list(args.keys or [])
    if args.only_key:
        user_keys.extend(
            k.strip() for k in args.only_key.split(",") if k.strip()
        )
    if user_keys:
        # Sanity check: did any of the given keys look like attachment
        # keys we've already seen in another paper's md? This catches
        # the common mistake of passing a storage/ subdir name (which
        # is an attachment key) where a paper key is expected.
        return _check_keys_not_attachments(cfg, set(user_keys))

    # Compute `imported` (purely local file scan, cheap).
    imported = {
        p.stem
        for p in cfg.papers_dir.glob("*.md")
        if not p.name.startswith(".")
    } if cfg.papers_dir.exists() else set()

    # --all-unprocessed: entirely local — scan md frontmatter for
    # fulltext_processed != true among already-imported papers. No
    # Zotero calls. This matters: real-machine measurement shows
    # reader.list_paper_keys() takes ~12 min on a 1150-paper web-mode
    # library, so a fulltext-only backfill that happens to hit that
    # code path would waste 12 min for nothing. Gated on flags so it
    # only runs when actually needed.
    unprocessed: set[str] = set()
    if args.all_unprocessed:
        from ..fulltext_writeback import is_fulltext_processed
        from ..md_builder import paper_md_path
        for key in imported:
            md = paper_md_path(cfg.kb_root, key)
            if not is_fulltext_processed(md):
                unprocessed.add(key)

    if not (args.collection or args.tag or args.year or args.all_pending
            or args.all_unprocessed):
        # No filter and no explicit keys — require --all-pending or
        # --all-unprocessed to be explicit about batch intent.
        print("Error: specify keys, a filter (--collection/--tag/--year), "
              "--all-pending, or --all-unprocessed.", file=sys.stderr)
        return set()

    # "Pending" = in Zotero but no md yet. Only compute when we
    # actually need it — fetching the Zotero side is the expensive
    # part of this whole function.
    needs_pending = (
        args.all_pending
        or (not args.all_unprocessed and (
            args.collection or args.tag or args.year
        ))
    )
    pending: set[str] = set()
    if needs_pending:
        all_keys = reader.list_paper_keys()
        pending = all_keys - imported

    # Assemble the base set.
    base_set: set[str]
    if args.all_pending and args.all_unprocessed:
        base_set = pending | unprocessed
    elif args.all_pending:
        base_set = pending
    elif args.all_unprocessed:
        base_set = unprocessed
    else:
        # A filter without --all-pending / --all-unprocessed still
        # operates on pending (backwards-compatible with pre-v22
        # behaviour).
        base_set = pending

    if not (args.year or args.collection or args.tag):
        return base_set

    # Filter by year / collection / tag. Requires fetching each item
    # which is slow for large libraries; OK for reasonable batch sizes.
    result: set[str] = set()
    for key in base_set:
        try:
            item = reader.get_paper(key)
        except Exception:
            continue
        if args.year and item.year != args.year:
            continue
        if args.collection and args.collection not in item.collections:
            continue
        if args.tag and args.tag not in item.tags:
            continue
        result.add(key)
    return result


def _resolve_note_keys(
    args: argparse.Namespace, cfg: Config, reader: ZoteroReader
) -> set[str]:
    user_keys: list[str] = list(args.keys or [])
    if args.only_key:
        user_keys.extend(
            k.strip() for k in args.only_key.split(",") if k.strip()
        )
    if user_keys:
        return set(user_keys)

    all_keys = reader.list_standalone_note_keys()
    imported = imported_note_keys(cfg)
    pending = all_keys - imported

    if not (args.tag or args.all_pending):
        print("Error: specify keys, --tag, or --all-pending.", file=sys.stderr)
        return set()

    if args.all_pending:
        return pending

    result: set[str] = set()
    for key in pending:
        try:
            item = reader.get_standalone_note(key)
        except Exception:
            continue
        if args.tag and args.tag not in item.tags:
            continue
        result.add(key)
    return result


# ----------------------------------------------------------------------
# Per-item processing
# ----------------------------------------------------------------------

def _process_paper(
    cfg: Config, reader: ZoteroReader, key: str, *, dry_run: bool
) -> None:
    item = reader.get_paper(key)
    path = paper_md_path(cfg.kb_root, key)
    preserved = extract_preserved(path)

    # Locate each attachment's PDF. Same order as item.attachments so
    # the md lists them in Zotero's natural order (main PDF first).
    attachment_locations: list[tuple] = []
    for att in item.attachments:
        pdf_abs, is_archived = find_pdf(cfg, att.key)
        rel_path: str | None = None
        if pdf_abs is not None:
            try:
                rel = pdf_abs.relative_to(cfg.storage_dir)
                rel_path = rel.as_posix()
                # Strip leading "_archived/" if present, for a stable
                # rel path regardless of archive state. (The archived
                # flag in the tuple tells the reader the truth.)
                if rel.parts and rel.parts[0] == "_archived":
                    rel_path = "/".join(rel.parts[1:])
            except ValueError:
                # PDF was outside storage_dir (shouldn't happen, but
                # be robust): fall back to filename only.
                rel_path = pdf_abs.name
        attachment_locations.append((att, rel_path, is_archived))

    md_text = build_paper_md(
        item,
        preserved=preserved,
        attachment_locations=attachment_locations,
    )

    if dry_run:
        return

    atomic_write(path, md_text)

    # Archive all unarchived attachment dirs in bulk.
    # Only move ones currently in storage/ (not already under _archived/).
    unarchived_keys = [
        att.key
        for att, _rel, is_archived in attachment_locations
        if not is_archived and (cfg.storage_dir / att.key).exists()
    ]
    if unarchived_keys:
        result = archive_attachments(cfg, unarchived_keys)
        if result.moved:
            log.info(
                "Archived %d attachment dirs for paper %s: %s",
                len(result.moved), key, ", ".join(result.moved),
            )
        for ak, reason in result.errors:
            log.warning(
                "Could not archive attachment %s (for paper %s): %s",
                ak, key, reason,
            )
        # already_there and not_found are expected/benign; don't log.


def _git_commit_enabled(args: argparse.Namespace) -> bool:
    """Honour --no-git-commit. Default on; any kb_root that isn't a
    git repo gets a silent no-op inside auto_commit itself, so we
    don't gate on is_git_repo here (auto_commit handles that and logs
    info-level).
    """
    return not getattr(args, "no_git_commit", False)


def _auto_commit_metadata_batch(
    cfg: Config,
    args: argparse.Namespace,
    written_keys: list[str],
) -> None:
    """Commit all md files written during the metadata pass as ONE
    git commit. Called at the end of the metadata loop.

    Rationale: metadata re-imports touch many paper mds at once
    (1000+ on a full-library run). One commit per paper would create
    log noise that buries the far more interesting per-paper fulltext
    and longform commits that run afterwards. The batch is still an
    atomic checkpoint — a crash mid-loop leaves the already-committed
    prefix in git and the rest un-committed in the working tree.

    No-op when:
      - --no-git-commit was passed
      - nothing was written (empty list)
      - kb_root isn't a git repo (auto_commit handles + logs)
    """
    if not _git_commit_enabled(args):
        return
    if not written_keys:
        return
    try:
        from kb_write.git import auto_commit, GitError
    except ImportError:
        # kb_write not installed (read-only / citations-only setups).
        # Silently skip — metadata files remain un-staged in the tree.
        log.info(
            "kb_write not available; skipping auto-commit of "
            "metadata batch (%d files).", len(written_keys),
        )
        return

    # Collect md paths. For a partial failure where build_paper_md
    # succeeded but the file was later deleted, auto_commit's `git
    # add` will silently skip (already-staged nonexistent paths just
    # produce no change) — safe.
    files: list[Path] = []
    if args.target == "papers":
        for k in written_keys:
            files.append(paper_md_path(cfg.kb_root, k))
    else:
        for k in written_keys:
            files.append(note_md_path(cfg.kb_root, k))

    target_label = (
        f"{args.target} batch ({len(written_keys)} item"
        f"{'s' if len(written_keys) != 1 else ''})"
    )
    try:
        sha = auto_commit(
            cfg.kb_root, files,
            op=f"import_{args.target}_metadata",
            target=target_label,
            message_body=(
                f"Keys ({len(written_keys)}): "
                + ", ".join(written_keys[:20])
                + (f", ... +{len(written_keys) - 20} more"
                   if len(written_keys) > 20 else "")
            ),
        )
        if sha:
            print(f"  git commit (metadata batch): {sha[:10]}")
    except GitError as e:
        # Commit failures shouldn't block the rest of the run (fulltext
        # pass, etc.). Loud warning is enough — the user will see
        # un-staged changes in `git status`.
        print(
            f"  ⚠  auto-commit of metadata batch failed: {e}. "
            f"Files are in working tree un-staged; commit manually.",
            file=sys.stderr,
        )


def _auto_commit_single_paper(
    cfg: Config,
    args: argparse.Namespace,
    paper_key: str,
    op: str,
    *,
    extra_files: list[Path] | None = None,
    message_body: str | None = None,
) -> None:
    """Commit one paper md's update (fulltext writeback) or one book's
    batch (longform: parent md + all chapter thoughts) as a SINGLE
    per-paper commit. Called after a successful writeback / ingest.

    op: "fulltext" for short-pipeline writeback,
        "longform"  for long-pipeline chapter ingest.
    extra_files: additional paths to stage beyond the paper md itself
        (e.g. the list of chapter thought files for longform).
    """
    if not _git_commit_enabled(args):
        return
    try:
        from kb_write.git import auto_commit, GitError
    except ImportError:
        return

    files: list[Path] = [paper_md_path(cfg.kb_root, paper_key)]
    if extra_files:
        files.extend(extra_files)

    try:
        sha = auto_commit(
            cfg.kb_root, files,
            op=f"{op}_{paper_key}",
            target=f"papers/{paper_key}",
            message_body=message_body,
        )
        if sha:
            log.debug("auto-committed %s/%s: %s", op, paper_key, sha[:10])
    except GitError as e:
        # Per-paper commit failure: warn but don't abort — next paper
        # might succeed. Files remain in working tree.
        print(
            f"  ⚠  {paper_key}  auto-commit ({op}) failed: {e}",
            file=sys.stderr,
        )


def _process_note(
    cfg: Config, reader: ZoteroReader, key: str, *, dry_run: bool
) -> None:
    item = reader.get_standalone_note(key)
    path = note_md_path(cfg.kb_root, key)

    preserved = extract_preserved(path)
    md_text = build_note_md(item, preserved=preserved)

    if dry_run:
        return

    atomic_write(path, md_text)


# ----------------------------------------------------------------------
# Fulltext pass
# ----------------------------------------------------------------------

def _run_fulltext_pass(
    args: argparse.Namespace,
    cfg: Config,
    reader: ZoteroReader,
    candidate_keys: set[str],
) -> int:
    """Extract fulltext + LLM summarise + writeback for each paper.

    Runs after metadata import. Work set = candidate_keys (already
    merged positional keys + --only-key + filters in _resolve_paper_keys)
    filtered by fulltext_processed state. Routing per paper is
    determined by its Zotero item_type via kb_importer.eligibility:

      - short → journal articles etc.: current 7-section pipeline.
      - long  → books / theses: chapter-splitting pipeline (stage 1+2
                of the long-form design; stage 3 global reduce is v23).
      - none  → webpages etc.: skipped, counted as skipped_ineligible.

    --longform / --no-longform override per-paper routing (diagnostic);
    --longform-dryrun runs only chapter detection without calling LLM.

    Does NOT talk to the MCP indexer — caller should run
    `kb-mcp index` after this to pick up new chunks.

    Returns 0 on clean run, 1 if any paper hit an unrecoverable error
    (missing LLM API key, LLM failed, writeback failed).
    """
    from ..fulltext import extract_fulltext, SOURCE_UNAVAILABLE
    from ..summarize import (
        build_provider_from_env, summarize_paper, SummarizerError,
        QuotaExhaustedError,
    )
    from ..fulltext_writeback import (
        is_fulltext_processed, writeback_summary,
    )
    from ..md_builder import paper_md_path
    from ..eligibility import fulltext_mode

    # Use candidate_keys directly — no re-filter by --only-key here.
    # The metadata phase already merged positional keys + --only-key
    # + filters into candidate_keys; filtering here would drift the
    # two phases' work sets apart (fixed in v22; previously a known
    # source of confusion where positional keys showed up in metadata
    # output but vanished from fulltext output).
    #
    # We only need: "did metadata import succeed?" (md file exists)
    # and "already processed?" (skip unless --force-fulltext).
    work: list[str] = []
    skipped_already_processed = 0
    skipped_missing_md = 0
    for key in sorted(candidate_keys):
        md_path = paper_md_path(cfg.kb_root, key)
        if not md_path.is_file():
            skipped_missing_md += 1
            continue
        if not args.force_fulltext and is_fulltext_processed(md_path):
            skipped_already_processed += 1
            continue
        work.append(key)

    if not work:
        print(
            f"\nFulltext: nothing to do "
            f"(skipped {skipped_already_processed} already processed, "
            f"{skipped_missing_md} missing md; pass --force-fulltext "
            f"to reprocess)."
        )
        return 0

    # LLM provider is shared between short and long pipelines. Skip
    # construction in dryrun (we don't call the LLM).
    provider = None
    if not args.longform_dryrun:
        try:
            provider = build_provider_from_env(
                args.fulltext_provider, args.fulltext_model,
            )
        except SummarizerError as e:
            print(f"\nFulltext: {e}", file=sys.stderr)
            return 1

    # Fallback state for daily-quota exhaustion. Shared between the
    # short and long pipelines so a daily-quota hit during the short
    # pass carries into the long pass (same session, same API key →
    # same quota pool). Structure:
    #   fallback_state["enabled"]  — True if user allowed fallback
    #   fallback_state["model"]    — name to switch to on first hit
    #   fallback_state["activated"]— True after the switch happened
    #   fallback_state["stop"]     — True when even the fallback
    #                                model hit quota; caller bails
    #                                out of remaining work.
    fallback_state: dict = {
        "enabled": (
            args.fulltext_provider == "gemini"
            and not args.no_fulltext_fallback
            and bool((args.fulltext_fallback_model or "").strip())
        ),
        "model": args.fulltext_fallback_model or "",
        "activated": False,
        "stop": False,
    }

    def _try_fallback_after_quota(
        err: QuotaExhaustedError, key: str,
    ) -> bool:
        """Decide whether to switch provider.model to the fallback
        model after a QuotaExhaustedError. Returns True if the caller
        should retry `key` on the new model; False if the quota hit
        was unrecoverable (either fallback disabled, already activated
        and hit again, or non-gemini provider).

        Side effects:
          - Mutates `provider.model` on activation (session-sticky).
          - Sets fallback_state["stop"] = True when the fallback
            itself hit quota — caller then exits the loop.
          - Rate-limit (per-minute) quotas are NOT a fallback trigger:
            the caller should sleep(retry_after) and retry same model.
        """
        # Per-minute quotas: short sleep + retry, don't switch.
        if err.quota_type == "rate":
            import time
            delay = err.retry_after if err.retry_after else 30.0
            delay = min(delay, 120.0)  # cap at 2 min to avoid hangs
            print(
                f"  … {key}  rate-limit ({err.model}); sleeping "
                f"{delay:.0f}s before retry",
                file=sys.stderr,
            )
            time.sleep(delay)
            return True
        # Daily (or unknown → treat as daily) quotas.
        if not fallback_state["enabled"]:
            print(
                f"  ✗ {key}  daily quota exhausted on {err.model}; "
                f"fallback disabled (--no-fulltext-fallback or empty "
                f"--fulltext-fallback-model). Stopping fulltext pass.",
                file=sys.stderr,
            )
            fallback_state["stop"] = True
            return False
        if fallback_state["activated"]:
            # Already switched once; the fallback itself just hit
            # quota. We deliberately don't chain further — per the
            # original design decision: "if 2.5-pro's daily quota
            # also runs out, stop; don't cascade down further
            # tiers". Stop the batch.
            print(
                f"  ✗ {key}  daily quota exhausted on fallback "
                f"{err.model} too. Stopping fulltext pass. Remaining "
                f"papers will need a separate run (e.g. tomorrow "
                f"after quota reset, or with a different API key).",
                file=sys.stderr,
            )
            fallback_state["stop"] = True
            return False
        # First activation.
        old_model = provider.model if provider else err.model
        try:
            provider.model = fallback_state["model"]
        except Exception:
            # Defensive: if provider is None (shouldn't be — we're in
            # the non-dryrun path) or immutable, bail cleanly.
            fallback_state["stop"] = True
            return False
        fallback_state["activated"] = True
        retry_note = (
            f" (primary retry window: {err.retry_after:.0f}s)"
            if err.retry_after else ""
        )
        print(
            f"  ↓ {key}  daily quota on {old_model}; switching to "
            f"{fallback_state['model']} for remaining papers"
            f"{retry_note}",
            file=sys.stderr,
        )
        return True

    # Classify each work item up front so the user sees short vs long
    # vs skipped counts before any LLM spend.
    mode_override = getattr(args, "longform_override", None)
    short_keys: list[str] = []
    long_keys: list[str] = []
    skipped_ineligible = 0
    ineligible_breakdown: dict[str, int] = {}
    for key in work:
        md_path = paper_md_path(cfg.kb_root, key)
        item_type = _peek_item_type(md_path)
        if mode_override:
            mode = mode_override
        else:
            mode = fulltext_mode(item_type)
        if mode == "short":
            short_keys.append(key)
        elif mode == "long":
            long_keys.append(key)
        else:
            skipped_ineligible += 1
            label = item_type or "(unknown)"
            ineligible_breakdown[label] = (
                ineligible_breakdown.get(label, 0) + 1
            )

    provider_label = (
        f"{provider.name}/{provider.model}" if provider else "(dryrun)"
    )
    print(
        f"\nFulltext pass: {len(short_keys)} short, "
        f"{len(long_keys)} long, "
        f"{skipped_ineligible} ineligible "
        f"via {provider_label}"
    )
    if skipped_ineligible:
        detail = ", ".join(
            f"{t}={n}" for t, n in sorted(ineligible_breakdown.items())
        )
        print(f"  ineligible breakdown: {detail}")

    # Aggregated counters across both pipelines.
    source_counts: dict[str, int] = {}
    llm_ok = 0
    llm_fail = 0
    extract_miss = 0
    skipped_longform_existing = 0  # v24: idempotency skip count
    total_prompt_tokens = 0
    total_completion_tokens = 0

    storage_dir = cfg.zotero_storage_dir if cfg.zotero_storage_dir else None

    # ---- Short pipeline ----
    # Defensive dedup. The upstream flow (set[str] → sorted → work →
    # short_keys) cannot produce duplicates today, but a pre-v19
    # version of the import flow enumerated at attachment level and
    # double-summarised papers with multiple PDFs. If a future
    # refactor reintroduces that shape, detect it here, loudly warn
    # the operator, dedup, and continue — raising instead would
    # abort the whole fulltext pass after short work may have
    # already completed, and burning a traceback is less useful
    # than a visible warning plus a correct run.
    if len(set(short_keys)) != len(short_keys):
        seen_s: dict[str, int] = {}
        for k in short_keys:
            seen_s[k] = seen_s.get(k, 0) + 1
        dupes_s = {k: n for k, n in seen_s.items() if n > 1}
        print(
            f"\n⚠  short_keys contained {len(dupes_s)} duplicate paper_key(s) "
            f"(total {sum(n - 1 for n in dupes_s.values())} extra entries): "
            f"{dupes_s}. Deduping and continuing — but this indicates an "
            f"upstream regression: paper-key assembly should be set-based. "
            f"Please report.",
            file=sys.stderr,
        )
        short_keys = list(dict.fromkeys(short_keys))
    for key in short_keys:
        md_path = paper_md_path(cfg.kb_root, key)
        try:
            paper = reader.get_paper(key)
        except Exception as e:
            print(f"  ✗ {key}  could not re-fetch item: {e}",
                  file=sys.stderr)
            extract_miss += 1
            continue

        result = extract_fulltext(
            paper_key=key,
            attachments=paper.attachments,
            reader=reader,
            storage_dir=storage_dir,
        )
        if not result.ok:
            print(f"  – {key}  extract miss ({result.error})")
            extract_miss += 1
            source_counts[SOURCE_UNAVAILABLE] = (
                source_counts.get(SOURCE_UNAVAILABLE, 0) + 1
            )
            # v26: record to skip log for periodic aggregation.
            # We categorise "no PDF at all" vs "PDF present but
            # unreadable" heuristically from result.error — if the
            # error string mentions pdfplumber/pypdf the attachment
            # was found but extraction failed.
            from ..events import (
                record_event, EVENT_FULLTEXT_SKIP,
                REASON_PDF_MISSING, REASON_PDF_UNREADABLE,
            )
            err_lower = (result.error or "").lower()
            if "pdfplumber" in err_lower or "pypdf" in err_lower:
                cat = REASON_PDF_UNREADABLE
            else:
                cat = REASON_PDF_MISSING
            record_event(
                cfg.kb_root,
                event_type=EVENT_FULLTEXT_SKIP,
                paper_key=key, category=cat,
                detail=result.error or "extract miss",
                pipeline="short",
            )
            continue

        if args.longform_dryrun:
            # For short papers, dryrun just reports what would happen.
            print(f"  (dryrun) {key}  [short] would summarise "
                  f"{len(result.text)} chars from {result.source}")
            continue

        authors_s = ", ".join(paper.authors or []) or ""
        # Quota-aware retry loop: if the current model hits quota, we
        # may (a) switch to fallback (daily) or (b) sleep and retry
        # (rate). At most 2 attempts — one primary, one on fallback.
        # A retry is only issued if _try_fallback_after_quota returned
        # True; on False we've already printed why and we abort the
        # paper (llm_fail++) and, if fallback_state["stop"] is set,
        # break out of the short-pipeline loop entirely.
        summary = None
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                summary = summarize_paper(
                    provider=provider,
                    fulltext=result.text,
                    title=paper.title or "",
                    authors=authors_s,
                    year=paper.year or "",
                    doi=paper.doi or "",
                    abstract=paper.abstract or "",
                    max_output_tokens=args.fulltext_max_tokens,
                )
                break  # success
            except QuotaExhaustedError as e:
                if _try_fallback_after_quota(e, key):
                    last_err = e
                    continue  # retry on new model / after sleep
                last_err = e
                break  # caller decided not to retry
            except SummarizerError as e:
                last_err = e
                break
            except Exception as e:
                log.exception("unexpected summariser error on %s", key)
                last_err = e
                break

        if summary is None:
            # v26: classify the error and write a structured event so
            # periodic aggregation (`kb-mcp report`) can say
            # "N quota, M bad-request, K unexpected" at a glance.
            # The stderr prints above stay for real-time feedback;
            # events.jsonl persistently records what was lost.
            from ..events import (
                record_event, EVENT_FULLTEXT_SKIP,
                REASON_QUOTA_EXHAUSTED, REASON_LLM_BAD_REQUEST,
                REASON_LLM_OTHER, REASON_OTHER,
            )
            _err_text = str(last_err) if last_err else ""
            # Gather provider/model metadata from fallback_state for
            # the log (we don't have a clean provider handle here
            # because the retry loop reassigns `provider`). Best-effort.
            _provider_name = fallback_state.get("provider")
            _model_tried = fallback_state.get("primary_model")
            _fallback_tried = fallback_state.get("fallback_model") if \
                fallback_state.get("stop") else None

            if isinstance(last_err, QuotaExhaustedError):
                # Message already printed by _try_fallback_after_quota.
                llm_fail += 1
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=REASON_QUOTA_EXHAUSTED,
                    detail=_err_text,
                    provider=_provider_name, model_tried=_model_tried,
                    fallback_tried=_fallback_tried, pipeline="short",
                )
            elif isinstance(last_err, SummarizerError):
                print(f"  ✗ {key}  LLM failed: {last_err}",
                      file=sys.stderr)
                llm_fail += 1
                # HTTP 400-ish errors surface in the message; split
                # them out so "summary is systematically broken for
                # this paper" (e.g. fulltext too long) is distinct
                # from "transient infra hiccup".
                if "400" in _err_text or "bad request" in _err_text.lower():
                    cat = REASON_LLM_BAD_REQUEST
                else:
                    cat = REASON_LLM_OTHER
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=cat, detail=_err_text,
                    provider=_provider_name, model_tried=_model_tried,
                    pipeline="short",
                )
            else:
                print(
                    f"  ✗ {key}  unexpected: "
                    f"{type(last_err).__name__}: {last_err}",
                    file=sys.stderr,
                )
                llm_fail += 1
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=REASON_OTHER,
                    detail=f"{type(last_err).__name__}: {_err_text}",
                    provider=_provider_name, model_tried=_model_tried,
                    pipeline="short",
                )
            if fallback_state["stop"]:
                # Exhausted both primary and fallback; stop the whole
                # short pipeline so we don't keep hot-looping 429s.
                print(
                    "\nFulltext short pipeline: halting early due to "
                    "exhausted quota. "
                    f"Completed {llm_ok}, failed {llm_fail}.",
                    file=sys.stderr,
                )
                break
            continue

        try:
            writeback_summary(
                md_path,
                summary_markdown=summary.to_markdown(),
                source=result.source,
                model_label=f"{summary.provider}/{summary.model}",
            )
        except Exception as e:
            log.exception("writeback failed for %s", key)
            print(f"  ✗ {key}  writeback: {type(e).__name__}: {e}",
                  file=sys.stderr)
            llm_fail += 1
            continue

        llm_ok += 1
        source_counts[result.source] = source_counts.get(result.source, 0) + 1
        total_prompt_tokens += summary.prompt_tokens
        total_completion_tokens += summary.completion_tokens
        print(f"  ✓ {key}  [short:{result.source}]  "
              f"in={summary.prompt_tokens} out={summary.completion_tokens}")

        # Per-paper auto-commit. Each successful fulltext writeback
        # gets its own commit — meaningful atomic unit (the md file
        # is self-contained, commit message records the model used),
        # and a mid-run crash leaves completed papers committed while
        # the rest stays re-runnable. No-op when --no-git-commit or
        # not a git repo. Commit failures warn but don't abort the
        # remaining loop.
        _auto_commit_single_paper(
            cfg, args, key, op="fulltext",
            message_body=(
                f"source: {result.source}\n"
                f"model: {summary.provider}/{summary.model}\n"
                f"tokens: in={summary.prompt_tokens} "
                f"out={summary.completion_tokens}"
            ),
        )

    # ---- Long pipeline ----
    if long_keys:
        # Same defensive dedup as short pipeline. See the note there
        # for why this is warn-and-dedup rather than raise — aborting
        # after short pipeline has already spent LLM budget is worse
        # than running long pipeline correctly with a prominent warning.
        if len(set(long_keys)) != len(long_keys):
            seen: dict[str, int] = {}
            for k in long_keys:
                seen[k] = seen.get(k, 0) + 1
            dupes = {k: n for k, n in seen.items() if n > 1}
            print(
                f"\n⚠  long_keys contained {len(dupes)} duplicate paper_key(s) "
                f"(total {sum(n - 1 for n in dupes.values())} extra entries): "
                f"{dupes}. Deduping and continuing — but this indicates an "
                f"upstream regression: paper-key assembly should be set-based. "
                f"Please report.",
                file=sys.stderr,
            )
            long_keys = list(dict.fromkeys(long_keys))
        from ..longform import (
            longform_ingest_paper, LongformError,
        )
        for key in long_keys:
            md_path = paper_md_path(cfg.kb_root, key)
            try:
                paper = reader.get_paper(key)
            except Exception as e:
                print(f"  ✗ {key}  could not re-fetch item: {e}",
                      file=sys.stderr)
                extract_miss += 1
                continue

            result = extract_fulltext(
                paper_key=key,
                attachments=paper.attachments,
                reader=reader,
                storage_dir=storage_dir,
                # Long pipeline needs the ENTIRE book text so
                # split_into_chapters can see all chapter markers.
                # Default truncate=True would drop the middle 30%
                # of any >200K-char book, making chapters 4-12 of
                # a 15-chapter book disappear silently.
                truncate=False,
            )
            if not result.ok:
                print(f"  – {key}  extract miss ({result.error})")
                extract_miss += 1
                source_counts[SOURCE_UNAVAILABLE] = (
                    source_counts.get(SOURCE_UNAVAILABLE, 0) + 1
                )
                from ..events import (
                    record_event, EVENT_FULLTEXT_SKIP,
                    REASON_PDF_MISSING, REASON_PDF_UNREADABLE,
                )
                err_lower = (result.error or "").lower()
                cat = REASON_PDF_UNREADABLE if (
                    "pdfplumber" in err_lower or "pypdf" in err_lower
                ) else REASON_PDF_MISSING
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=cat,
                    detail=result.error or "extract miss",
                    pipeline="long",
                )
                continue

            # Quota-aware retry loop, same shape as short pipeline.
            # longform_ingest_paper internally calls provider.complete
            # per chapter; QuotaExhaustedError from any chapter bubbles
            # up here. _try_fallback_after_quota mutates provider.model
            # in place, so the retry runs on the new (fallback) model.
            outcome = None
            last_err: Exception | None = None
            for _attempt in range(2):
                try:
                    outcome = longform_ingest_paper(
                        cfg=cfg,
                        paper_key=key,
                        paper=paper,
                        fulltext=result.text,
                        pdf_path=result.pdf_path,
                        provider=provider,
                        max_output_tokens=args.fulltext_max_tokens,
                        dryrun=args.longform_dryrun,
                        # --force-fulltext overrides the idempotency
                        # skip. Without --force, a paper whose
                        # chapter thoughts already exist on disk is
                        # skipped (no LLM spend).
                        force_regenerate=args.force_fulltext,
                    )
                    break
                except QuotaExhaustedError as e:
                    if _try_fallback_after_quota(e, key):
                        last_err = e
                        continue
                    last_err = e
                    break
                except LongformError as e:
                    last_err = e
                    break
                except Exception as e:
                    log.exception("unexpected longform error on %s", key)
                    last_err = e
                    break

            if outcome is None:
                from ..events import (
                    record_event, EVENT_FULLTEXT_SKIP,
                    REASON_QUOTA_EXHAUSTED, REASON_LONGFORM_FAILURE,
                    REASON_OTHER,
                )
                _err_text = str(last_err) if last_err else ""
                _provider_name = fallback_state.get("provider")
                _model_tried = fallback_state.get("primary_model")
                _fallback_tried = fallback_state.get("fallback_model") if \
                    fallback_state.get("stop") else None

                if isinstance(last_err, QuotaExhaustedError):
                    llm_fail += 1
                    record_event(
                        cfg.kb_root,
                        event_type=EVENT_FULLTEXT_SKIP,
                        paper_key=key, category=REASON_QUOTA_EXHAUSTED,
                        detail=_err_text,
                        provider=_provider_name, model_tried=_model_tried,
                        fallback_tried=_fallback_tried, pipeline="long",
                    )
                elif isinstance(last_err, LongformError):
                    print(f"  ✗ {key}  longform failed: {last_err}",
                          file=sys.stderr)
                    llm_fail += 1
                    record_event(
                        cfg.kb_root,
                        event_type=EVENT_FULLTEXT_SKIP,
                        paper_key=key, category=REASON_LONGFORM_FAILURE,
                        detail=_err_text,
                        provider=_provider_name, model_tried=_model_tried,
                        pipeline="long",
                    )
                else:
                    print(
                        f"  ✗ {key}  unexpected: "
                        f"{type(last_err).__name__}: {last_err}",
                        file=sys.stderr,
                    )
                    llm_fail += 1
                    record_event(
                        cfg.kb_root,
                        event_type=EVENT_FULLTEXT_SKIP,
                        paper_key=key, category=REASON_OTHER,
                        detail=f"{type(last_err).__name__}: {_err_text}",
                        provider=_provider_name, model_tried=_model_tried,
                        pipeline="long",
                    )
                if fallback_state["stop"]:
                    print(
                        "\nFulltext long pipeline: halting early due "
                        "to exhausted quota. "
                        f"Completed {llm_ok}, failed {llm_fail}.",
                        file=sys.stderr,
                    )
                    break
                continue

            if args.longform_dryrun:
                print(
                    f"  (dryrun) {key}  [long] "
                    f"{len(outcome.chapters)} chapters via "
                    f"{outcome.split_source}"
                )
                for ch in outcome.chapters[:10]:
                    title = (ch.title or "(untitled)")[:60]
                    print(f"      ch{ch.number:02d}: {title}")
                if len(outcome.chapters) > 10:
                    print(f"      ... +{len(outcome.chapters) - 10} more")
                continue

            # Idempotency skip: existing chapter thoughts on disk,
            # --force-fulltext not set. longform_ingest_paper returns
            # an empty outcome with split_source="skipped_idempotent"
            # in this case (no LLM spend, no file writes). Don't
            # count as success OR failure — it's "already done".
            if outcome.split_source == "skipped_idempotent":
                skipped_longform_existing += 1
                print(
                    f"  — {key}  [long] already has chapter "
                    f"thoughts on disk; skipping "
                    f"(pass --force-fulltext to regenerate)"
                )
                continue

            llm_ok += 1
            source_counts[result.source] = (
                source_counts.get(result.source, 0) + 1
            )
            total_prompt_tokens += outcome.prompt_tokens
            total_completion_tokens += outcome.completion_tokens
            print(
                f"  ✓ {key}  [long:{outcome.split_source}] "
                f"{outcome.chapters_written} chapters, "
                f"in={outcome.prompt_tokens} out={outcome.completion_tokens}"
            )

            # Per-book auto-commit: one commit encompassing the parent
            # paper md (with its chapter-index writeback) AND every
            # chapter thought file produced in this run. This keeps a
            # book's ingest as one atomic unit in git history —
            # reverting a single commit undoes the entire longform
            # ingest cleanly. Per-chapter commits would make revert
            # painful and bloat `git log` for 15-50 chapter books.
            chapter_paths = [
                co.thought_path for co in outcome.per_chapter
                if getattr(co, "thought_path", None) is not None
            ]
            _auto_commit_single_paper(
                cfg, args, key, op="longform",
                extra_files=chapter_paths,
                message_body=(
                    f"split: {outcome.split_source}\n"
                    f"chapters: {outcome.chapters_written}\n"
                    f"model: {provider.name}/{provider.model}\n"
                    f"tokens: in={outcome.prompt_tokens} "
                    f"out={outcome.completion_tokens}"
                ),
            )

    # ---- Final report ----
    print(f"\nFulltext done: "
          f"{llm_ok} summarised, "
          f"{extract_miss} extract-miss, "
          f"{llm_fail} llm-fail, "
          f"{skipped_ineligible} ineligible")
    if skipped_longform_existing:
        print(f"  (longform idempotency: skipped "
              f"{skipped_longform_existing} book(s) already ingested; "
              f"pass --force-fulltext to regenerate)")
    if source_counts:
        print("  sources: " + ", ".join(
            f"{k}={v}" for k, v in sorted(source_counts.items())
        ))
    print(f"  tokens: prompt={total_prompt_tokens}, "
          f"completion={total_completion_tokens}")
    print("  next: run `kb-mcp index` to pick up the new chunks.")
    return 0 if llm_fail == 0 else 1


def _peek_item_type(md_path: Path) -> str:
    """Read just the item_type field from an md's frontmatter.

    Thin wrapper around md_io.peek_frontmatter. Empty string means
    "unknown" — fulltext_mode() treats it as the conservative
    "short" default.
    """
    from ..md_io import peek_frontmatter
    meta = peek_frontmatter(md_path)
    if meta is None:
        return ""
    v = meta.get("item_type")
    return str(v) if v else ""
