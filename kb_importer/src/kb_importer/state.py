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
repo. We do NOT derive this from scanning `storage/`, because that
directory is organised by attachment keys (not paper keys) and an
attachment subdir can exist without its md being written (if a
process was interrupted between steps).

## API

- `imported_paper_keys(cfg)`, `imported_note_keys(cfg)`:
  Read from `papers/` and `topics/standalone-note/` — the
  authoritative source.
- `scan_attachments(cfg)`:
  List attachment-key subdirs under `storage/`. Useful for `status`
  display, NOT for deciding which papers are imported.
- `find_pdf(cfg, attachment_key)`:
  Locate the PDF file for ONE attachment.

## 0.29.1: _archived removed entirely

Before 0.29.0, each successful paper import moved
`storage/{attachment_key}/` into `storage/_archived/{attachment_key}/`,
nominally to keep `ls storage/` tidy. 0.29.0 neutered the
auto-archive step (made it a no-op) and left `find_pdf()` with a
back-compat fallback. 0.29.1 removes the feature entirely: no
`_archived/` traversal, no `archive_attachments()` /
`unarchive_attachments()` helpers, no `ArchiveResult` / `ARCHIVE_SUBDIR`
/ `Config.archive_dir`. Personal-testing only; no back-compat with
libraries that still have PDFs under `_archived/` — operators must
flatten those manually (e.g. `mv storage/_archived/*/ storage/`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


@dataclass
class AttachmentScan:
    """Snapshot of `storage/` attachment-key directories.

    Contains ATTACHMENT keys (not paper keys). For paper-level
    progress, use `imported_paper_keys()`.

    0.29.1: previously split into unarchived + archived sets.
    Collapsed to a single set after _archived was removed.
    """

    dirs: set[str] = field(default_factory=set)


def scan_attachments(cfg: Config) -> AttachmentScan:
    """List attachment-key directories under `storage/`.

    Hidden directories and non-directory entries are skipped. Does
    NOT tell you which PAPERS are imported — use `imported_paper_keys`
    for that.
    """
    scan = AttachmentScan()
    storage = cfg.storage_dir
    if storage.exists():
        for child in storage.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            scan.dirs.add(child.name)
    return scan


def note_is_imported(cfg: Config, zotero_key: str) -> bool:
    """A standalone note counts as imported iff its md exists."""
    return (cfg.notes_dir / f"{zotero_key}.md").exists()


def paper_is_imported(cfg: Config, paper_key: str) -> bool:
    """A paper counts as imported iff its md exists."""
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
# Finding individual PDFs
# ---------------------------------------------------------------------

def find_pdf(cfg: Config, attachment_key: str) -> Path | None:
    """Locate the PDF file for one attachment key.

    Returns the .pdf file on disk, or None if not found.

    A Zotero attachment subdirectory typically holds exactly one PDF
    plus a few small metadata files (.zotero-ft-cache, etc.). If
    there are multiple PDFs, the lexicographically first one is
    returned.

    0.29.1: signature changed from `tuple[Path | None, bool]` to
    `Path | None` (removed is_archived flag). All callers updated.
    """
    base = cfg.storage_dir / attachment_key
    if not base.exists() or not base.is_dir():
        return None
    for p in sorted(base.iterdir()):
        if p.is_file() and p.suffix.lower() == ".pdf":
            return p
    return None
