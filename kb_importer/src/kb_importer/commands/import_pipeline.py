"""Per-item processing for `kb-importer import`.

Extracted from import_cmd.py in v0.28.0. Paper / note processing
and the per-run auto-commit logic.
"""
from __future__ import annotations

import argparse
import logging

from ..config import Config
from ..md_builder import (
    build_note_md,
    build_paper_md,
    note_md_path,
    paper_md_path,
)
from ..md_io import atomic_write, extract_preserved
from ..state import (
    # archive_attachments intentionally NOT imported in 0.29.0 —
    # auto-archive was removed. find_pdf() still resolves through
    # both storage/ and storage/_archived/ for read compatibility.
    find_pdf,
)
from ..zotero_reader import ZoteroItem, ZoteroReader

log = logging.getLogger(__name__)


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

    # v0.29.0: auto-archive was removed. Pre-0.29, each successful
    # import moved the paper's attachment dirs from storage/ to
    # storage/_archived/. Combined with the _fetch_children swallow
    # bug (also fixed in 0.29), this produced an endless attachment
    # thrash: transient Zotero API errors would make papers look
    # attachment-less, kb-importer would re-run and un-archive,
    # then on the next successful fetch re-archive, each round
    # bumping md mtimes and kb-mcp reindex work. The archive step
    # added operational complexity for no user-visible benefit —
    # attachments are keyed by Zotero key, not by whether they
    # live in storage/ or _archived/. find_pdf() still looks in
    # both locations so existing _archived/ contents remain
    # reachable; we just don't move anything anymore.
    #
    # Operators who want to reclaim disk space from _archived/ can
    # inspect and delete manually — `kb-importer orphans` still
    # reports what's there.


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


