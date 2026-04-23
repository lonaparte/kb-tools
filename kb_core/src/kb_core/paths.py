"""Path-safety and KB directory layout.

One source of truth for:

- `safe_resolve(kb_root, rel)` — the canonical "resolve a kb-rel
  path against kb_root, reject escapes" function.
- `to_relative(kb_root, abs_path)` — the inverse formatter.
- `is_book_chapter_filename` — v26 book-chapter filename parser.
- The KB directory-layout constants (`PAPERS_DIR`, etc.) that every
  package imports from.

Previously each of kb_write and kb_mcp kept a copy of these,
synced by a lint. In v27 this is the one real copy; kb_write and
kb_mcp re-export for backward compatibility.
"""
from __future__ import annotations

import re
from pathlib import Path


class PathError(Exception):
    """Raised when a requested path is malformed or escapes the KB.

    The single canonical class — both kb_write.paths.PathError and
    kb_mcp.paths.PathError re-export THIS class, so `except
    PathError:` catches all three equivalently.
    """


def safe_resolve(kb_root: Path, rel: str) -> Path:
    """Resolve a KB-relative path to an absolute path, rejecting any
    result that escapes kb_root.

    Behaviour:
      - Empty / whitespace-only input → PathError.
      - POSIX absolute path (leading `/` or `\\`) → PathError.
      - Windows drive-letter path (second char `:`) → PathError.
        This runs regardless of host OS — on POSIX a path starting
        with `C:` is "legal" (a filename) but almost certainly a
        cross-platform-copy mistake or injection attempt, and we
        want identical rejection semantics across all tools.
      - Backslashes normalised to forward slashes before resolve.
      - Escape via `..` or symlink caught at the relative_to() step.
    """
    if not rel:
        raise PathError("empty path not allowed")

    # Reject absolute paths and Windows drive letters BEFORE any
    # normalisation, so a crafted path like "\\C:\\Windows" can't
    # sneak through via replace(\\, /).
    if rel.startswith(("/", "\\")):
        raise PathError(f"absolute path not allowed: {rel!r}")
    if len(rel) >= 2 and rel[1] == ":":
        raise PathError(f"drive-letter path not allowed: {rel!r}")

    cleaned = rel.replace("\\", "/")
    if not cleaned:
        raise PathError("empty path after normalisation")

    root = kb_root.resolve()
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PathError(f"{rel!r} escapes kb_root")
    return candidate


def to_relative(kb_root: Path, abs_path: Path) -> str:
    """Format an absolute path under kb_root as a POSIX-style relative
    string, suitable for display and for round-tripping through
    `safe_resolve`. Raises ValueError if abs_path escapes kb_root.
    """
    rel = abs_path.resolve().relative_to(kb_root.resolve())
    return rel.as_posix()


# ----------------------------------------------------------------------
# KB directory layout (v26, unchanged in v27).
#
# Breaking changes from v25 (no auto-migration):
#   - zotero-notes/        → topics/standalone-note/
#   - topics/<slug>.md     → topics/agent-created/<slug>.md
#   - thoughts/<date>-<bookkey>-ch<NN>-*.md (book chapter products)
#                          → papers/<BOOKKEY>-ch<NN>.md (shared key)
# Contents at the old locations are NOT automatically moved — the
# indexer skips them, and index-status flags them so the user (or
# agent) can reorganise.
# ----------------------------------------------------------------------

PAPERS_DIR            = "papers"
TOPICS_STANDALONE_DIR = "topics/standalone-note"
TOPICS_AGENT_DIR      = "topics/agent-created"
THOUGHTS_DIR          = "thoughts"

# All currently-valid subdirs, in scan order for the indexer.
# Note: we DO NOT scan top-level "topics/" directly — that would
# double-scan contents of standalone-note/ and agent-created/.
# Instead we scan the two sub-buckets as distinct subdirs.
ACTIVE_SUBDIRS = (
    PAPERS_DIR,
    TOPICS_STANDALONE_DIR,
    TOPICS_AGENT_DIR,
    THOUGHTS_DIR,
)


_BOOK_CHAPTER_RE = re.compile(r"^(.+)-ch(\d+)\.md$")


def is_book_chapter_filename(filename: str) -> tuple[str, int] | None:
    """If `filename` is of the form `<KEY>-ch<NN>.md`, return
    `(parent_key, chapter_number)`. Else None.

    v26 convention: book / long-article chapters live as papers/
    siblings sharing the parent Zotero key, with a `-chNN` suffix
    (e.g. `papers/BOOKKEY.md` is the whole book; `papers/BOOKKEY-ch03.md`
    is chapter 3). Chapter numbers are zero-padded 2 digits minimum
    but we tolerate 1+ digits.
    """
    m = _BOOK_CHAPTER_RE.match(filename)
    if not m:
        return None
    return (m.group(1), int(m.group(2)))
