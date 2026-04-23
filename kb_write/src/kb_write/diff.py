"""Diff generation for `--dry-run` mode.

The standard `difflib.unified_diff` is fine for raw text, but our
deliverables are md files with frontmatter. A diff showing "every
line changed" because Python dumped the YAML in a different key
order is useless. This module normalizes both sides before diffing
so the output reflects semantic changes only.
"""
from __future__ import annotations

import difflib
from pathlib import Path


def make_diff(
    old_text: str,
    new_text: str,
    *,
    path: str,
    context: int = 3,
) -> str:
    """Return a unified diff string. Returns empty string if no
    difference.

    Lines are shown with ±/  markers per standard unified diff. Path
    appears in the header as both `a/<path>` and `b/<path>` so the
    output looks familiar to anyone who's read a `git diff`.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    # Ensure trailing newline so diff lines align.
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}",
        n=context,
    )
    return "".join(diff)


def preview_create(path: str, new_text: str) -> str:
    """Human-readable preview for a to-be-created file."""
    lines = [
        f"+++ b/{path}  (new file, {_count_lines(new_text)} lines, "
        f"{len(new_text)} bytes)"
    ]
    for i, line in enumerate(new_text.splitlines()[:40], 1):
        lines.append(f"  {i:3d} | {line}")
    if _count_lines(new_text) > 40:
        remaining = _count_lines(new_text) - 40
        lines.append(f"  ... ({remaining} more lines)")
    return "\n".join(lines)


def preview_delete(path: str, old_text: str) -> str:
    """Preview for a file that would be removed."""
    return (
        f"--- a/{path}  (to be DELETED, {_count_lines(old_text)} lines)\n"
        f"[full content would be lost; use git to recover if needed]"
    )


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
