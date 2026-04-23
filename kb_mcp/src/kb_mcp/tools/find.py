"""Tool: find_paper_by_key.

v26: Locate a paper md by its Zotero key.

Returns the WHOLE-WORK md — i.e. `papers/<KEY>.md`. If the Zotero key
is the parent of a multi-md work (a book split across chapter mds),
this function returns the parent `papers/<KEY>.md`; to list the
chapters, use `list_paper_parts`.

Also falls back to `topics/standalone-note/<KEY>.md` (v26 location,
was `zotero-notes/<KEY>.md` in v25) if the key belongs to a
standalone Zotero note.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..paths import PAPERS_DIR, TOPICS_STANDALONE_DIR
from .read import read_md_impl


# Zotero keys are 8 uppercase alphanumeric chars. Reject anything else
# early — it'd never match and may indicate an input mistake.
_ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{4,}$")


def find_paper_by_key_impl(kb_root: Path, zotero_key: str) -> str:
    """Return the md for a paper/note matching the Zotero key.

    Checks papers/{key}.md first (whole-work entry), then
    topics/standalone-note/{key}.md. If neither exists, returns an
    explicit "not found" message so the AI can decide to fall back
    to a different tool.

    v26 note: if the key is the parent of a multi-md work (e.g. a
    book split into chapters), only the parent md is returned.
    Use `list_paper_parts(key)` to enumerate chapter siblings.
    """
    key = (zotero_key or "").strip()
    if not key:
        return "[error] empty zotero_key"
    if not _ZOTERO_KEY_RE.match(key):
        return (
            f"[error] {key!r} does not look like a Zotero key "
            f"(expected uppercase alphanumeric). "
            f"Try query_by_meta or grep_md instead."
        )

    # Try papers/ first (most common). Only the WHOLE-WORK md —
    # chapters live at papers/<KEY>-chNN.md and are not matched
    # by this key lookup (they'd be found via list_paper_parts).
    paper_rel = f"{PAPERS_DIR}/{key}.md"
    if (kb_root / PAPERS_DIR / f"{key}.md").exists():
        return read_md_impl(kb_root, paper_rel)

    note_rel = f"{TOPICS_STANDALONE_DIR}/{key}.md"
    if (kb_root / TOPICS_STANDALONE_DIR / f"{key}.md").exists():
        return read_md_impl(kb_root, note_rel)

    return (
        f"[not found] no md for key {key}. "
        f"It may not be imported yet, or may be a child note "
        f"(embedded in a paper md). Try grep_md for the key string."
    )


def list_paper_parts_impl(kb_root: Path, zotero_key: str) -> str:
    """List all md files under papers/ that belong to the given
    Zotero key — the whole-work md plus any chapter mds.

    v26: for single-md papers this returns one path. For works
    split into chapters (book, long thesis), it returns the parent
    plus every `<KEY>-chNN.md` sibling. Ordered: parent first,
    then chapters by number.

    Returns a human-readable string with one path per line. If
    nothing matches, returns "[not found] ...".
    """
    key = (zotero_key or "").strip()
    if not key:
        return "[error] empty zotero_key"
    if not _ZOTERO_KEY_RE.match(key):
        return (
            f"[error] {key!r} does not look like a Zotero key "
            f"(expected uppercase alphanumeric)."
        )

    papers_dir = kb_root / PAPERS_DIR
    if not papers_dir.is_dir():
        return "[not found] no papers/ directory"

    parent = papers_dir / f"{key}.md"
    # Chapters: files matching `<KEY>-ch<digits>.md`.
    chapter_re = re.compile(
        r"^" + re.escape(key) + r"-ch(\d+)\.md$"
    )
    chapters: list[tuple[int, Path]] = []
    for md in papers_dir.glob(f"{key}-ch*.md"):
        m = chapter_re.match(md.name)
        if m:
            chapters.append((int(m.group(1)), md))
    chapters.sort()

    if not parent.exists() and not chapters:
        return (
            f"[not found] no md matching key {key} in papers/ "
            f"(tried '{key}.md' and '{key}-ch*.md')."
        )

    lines: list[str] = [f"Parts for Zotero key {key}:"]
    if parent.exists():
        lines.append(f"  - {PAPERS_DIR}/{key}.md  (whole work)")
    else:
        lines.append(
            f"  (no '{key}.md' whole-work md — only chapters present)"
        )
    for num, md in chapters:
        lines.append(f"  - {PAPERS_DIR}/{md.name}  (chapter {num})")
    return "\n".join(lines)
