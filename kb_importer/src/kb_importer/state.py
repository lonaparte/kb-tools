"""Filesystem-based progress tracking.

## The attachment-key vs paper-key distinction

This is the single most important thing to understand about this
module. Zotero's `storage/` directory has subdirectories named by
**attachment item keys**, NOT paper item keys. A single paper can have
multiple attachments (main PDF, supplementary PDF, annotated copy),
each with its own key and its own subdirectory.

So:
- `cfg.storage_dir / "XY7ZK3A2"` might hold one PDF of paper ABCD1234
- `cfg.storage_dir / "PQ4MN8R1"` might hold another PDF of the same paper
- There is NO directory named after "ABCD1234" itself in storage

## Progress tracking

"Paper X is imported" iff `papers/{paper_key}.md` exists in the KB
repo. We do NOT derive this from `storage/_archived/`, because that
directory is organised by attachment keys and an attachment can be
archived without its md being written (if the process was interrupted
between steps).

`storage/_archived/` is a *side-effect* of a successful import: after
writing the md, we move each of the paper's attachment subdirectories
under `_archived/` so they're out of the way of a future `list
pending` scan. Lose the archive dir and the md still exists; delete
the md and `unarchive` can put the attachments back in the path that
`list pending` will see.

## API

- `imported_paper_keys(cfg)`, `imported_note_keys(cfg)`:
  Read from `papers/` and `topics/standalone-note/` (v26; v25 was
  `zotero-notes/`) — the authoritative source.

- `archived_attachment_keys(cfg)`, `unarchived_attachment_keys(cfg)`:
  Read from the two halves of `storage/`. Useful for `status` display
  but NOT for deciding which papers are imported.

- `find_pdf(cfg, attachment_key)`:
  Locate the PDF file for ONE attachment. Tries both unarchived and
  archived locations.

- `archive_attachments(cfg, attachment_keys)` /
  `unarchive_attachments(cfg, attachment_keys)`:
  Bulk move a list of attachment directories. Idempotent on per-dir
  basis; reports which ones succeeded/failed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


ARCHIVE_SUBDIR = "_archived"


@dataclass
class AttachmentScan:
    """Snapshot of `storage/` attachment-key directories.

    Both sets contain ATTACHMENT keys (not paper keys). For
    paper-level progress, use `imported_paper_keys()`.
    """

    unarchived: set[str] = field(default_factory=set)   # attachment keys still in storage/
    archived: set[str] = field(default_factory=set)     # attachment keys under _archived/


def scan_attachments(cfg: Config) -> AttachmentScan:
    """List attachment-key directories under storage/ and _archived/.

    Both halves are reported separately. Hidden directories and any
    non-directory entries are skipped. The `_archived` dir itself is
    excluded from the `unarchived` set.

    Note: this does NOT tell you which PAPERS are imported. An
    attachment's presence under _archived/ means the last import
    touched it, but a paper with no PDF attachments will never appear
    here.
    """
    scan = AttachmentScan()

    storage = cfg.storage_dir
    if storage.exists():
        for child in storage.iterdir():
            if not child.is_dir():
                continue
            if child.name == ARCHIVE_SUBDIR:
                continue
            if child.name.startswith("."):
                continue
            scan.unarchived.add(child.name)

    archive = cfg.archive_dir
    if archive.exists():
        for child in archive.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            scan.archived.add(child.name)

    return scan


def note_is_imported(cfg: Config, zotero_key: str) -> bool:
    """A standalone note counts as imported iff its md exists."""
    return (cfg.notes_dir / f"{zotero_key}.md").exists()


def paper_is_imported(cfg: Config, paper_key: str) -> bool:
    """A paper counts as imported iff its md exists.

    This is the authoritative answer — do NOT check `_archived/` for
    this purpose (archive is attachment-keyed, not paper-keyed).
    """
    return (cfg.papers_dir / f"{paper_key}.md").exists()


def imported_note_keys(cfg: Config) -> set[str]:
    """All standalone-note keys that have md files in
    topics/standalone-note/ (v26; was zotero-notes/ in v25).
    """
    if not cfg.notes_dir.exists():
        return set()
    return {p.stem for p in cfg.notes_dir.glob("*.md") if not p.name.startswith(".")}


def imported_paper_keys(cfg: Config) -> set[str]:
    """All paper keys with md files in papers/."""
    if not cfg.papers_dir.exists():
        return set()
    return {p.stem for p in cfg.papers_dir.glob("*.md") if not p.name.startswith(".")}


# ---------------------------------------------------------------------
# Attachment archival (moves whole attachment-key directories)
# ---------------------------------------------------------------------

@dataclass
class ArchiveResult:
    """Outcome of a bulk archive/unarchive operation."""
    moved: list[str] = field(default_factory=list)       # attachment keys successfully moved
    already_there: list[str] = field(default_factory=list)  # dst existed; no-op
    not_found: list[str] = field(default_factory=list)   # src didn't exist
    errors: list[tuple[str, str]] = field(default_factory=list)  # (key, reason)


def archive_attachments(
    cfg: Config, attachment_keys: list[str]
) -> ArchiveResult:
    """Move storage/{ak}/ → _archived/{ak}/ for each attachment key.

    Per-key idempotent:
    - if src doesn't exist AND dst already exists → `already_there`
    - if src doesn't exist AND dst doesn't either → `not_found`
    - if src exists AND dst exists → `errors` (won't overwrite)
    - otherwise move and record in `moved`

    Does NOT fail on first error; tries every key so caller can see
    the full picture.
    """
    result = ArchiveResult()
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)

    for ak in attachment_keys:
        src = cfg.storage_dir / ak
        dst = cfg.archive_dir / ak

        if src.exists() and dst.exists():
            result.errors.append(
                (ak, f"both src and dst exist; refusing to overwrite {dst}")
            )
            continue
        if not src.exists() and dst.exists():
            result.already_there.append(ak)
            continue
        if not src.exists() and not dst.exists():
            result.not_found.append(ak)
            continue

        try:
            src.rename(dst)
            result.moved.append(ak)
        except OSError as e:
            result.errors.append((ak, str(e)))

    return result


def unarchive_attachments(
    cfg: Config, attachment_keys: list[str]
) -> ArchiveResult:
    """Inverse of archive_attachments."""
    result = ArchiveResult()

    for ak in attachment_keys:
        src = cfg.archive_dir / ak
        dst = cfg.storage_dir / ak

        if src.exists() and dst.exists():
            result.errors.append(
                (ak, f"both src and dst exist; refusing to overwrite {dst}")
            )
            continue
        if not src.exists() and dst.exists():
            result.already_there.append(ak)
            continue
        if not src.exists() and not dst.exists():
            result.not_found.append(ak)
            continue

        try:
            src.rename(dst)
            result.moved.append(ak)
        except OSError as e:
            result.errors.append((ak, str(e)))

    return result


# ---------------------------------------------------------------------
# Finding individual PDFs
# ---------------------------------------------------------------------

def find_pdf(cfg: Config, attachment_key: str) -> tuple[Path | None, bool]:
    """Locate the PDF file for one attachment key.

    Returns (path, is_archived):
    - path: the .pdf file on disk, or None if not found.
    - is_archived: True iff path was found under _archived/.

    A Zotero attachment subdirectory typically holds exactly one PDF
    plus a few small metadata files (.zotero-ft-cache, etc.). If there
    are multiple PDFs, the lexicographically first one is returned.
    """
    for base, is_archived in (
        (cfg.storage_dir / attachment_key, False),
        (cfg.archive_dir / attachment_key, True),
    ):
        if not base.exists() or not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.is_file() and p.suffix.lower() == ".pdf":
                return p, is_archived
    return None, False
