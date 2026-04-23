"""Tool: read_md.

Fast, deterministic file read. No parsing of frontmatter — the caller
(AI) can parse the YAML itself if needed. This keeps the tool surface
minimal and predictable.
"""
from __future__ import annotations

from pathlib import Path

from ..paths import PathError, safe_resolve


MAX_READ_BYTES = 2_000_000  # ~2 MB hard cap; protects against surprises.


def read_md_impl(kb_root: Path, md_path: str) -> str:
    """Return the complete text of a md file under kb_root.

    Args:
        kb_root: Repo root.
        md_path: Path relative to kb_root, e.g. "papers/ABCD1234.md".

    Returns:
        File content as string. On any error, returns a short "[error]"
        message rather than raising — MCP tools should degrade gracefully.
    """
    try:
        abs_path = safe_resolve(kb_root, md_path)
    except PathError as e:
        return f"[error] {e}"

    if not abs_path.exists():
        return f"[error] not found: {md_path}"
    if not abs_path.is_file():
        return f"[error] not a file: {md_path}"
    if abs_path.suffix != ".md":
        return f"[error] not a markdown file: {md_path}"

    try:
        stat = abs_path.stat()
        size = stat.st_size
        if size > MAX_READ_BYTES:
            return (
                f"[error] file too large ({size} bytes; limit {MAX_READ_BYTES}). "
                f"Use grep_md or list_files for large files."
            )
        content = abs_path.read_text(encoding="utf-8")
        # Prepend a single-line mtime header so agents can pass
        # expected_mtime to update_* tools without a separate
        # list_files/stat round-trip. The marker is parseable
        # (fixed prefix) but visually unobtrusive. 9 decimal places
        # = nanosecond precision, matches the format used by write
        # tool output and the atomic module's conflict messages —
        # round-tripping this string through float() preserves the
        # value exactly on all POSIX filesystems we care about.
        return f"<!-- mtime: {stat.st_mtime:.9f} -->\n{content}"
    except UnicodeDecodeError:
        return f"[error] not UTF-8 text: {md_path}"
    except OSError as e:
        return f"[error] read failed: {e}"
