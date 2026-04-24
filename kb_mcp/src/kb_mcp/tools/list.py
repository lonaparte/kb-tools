"""Tool: list_files.

Walk KB root (or a subdir) and return md file paths with minimal
metadata (size, optional frontmatter `kind`). No SQLite dependency —
we stat and optionally peek at frontmatter for each file.

For KBs with tens of thousands of md files, this will get slow. That's
fine for Phase 1 — the Indexer + query_by_meta tool will supersede this
once available.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter

from ..paths import PathError, safe_resolve


# Max files to return. Protects Claude's context window from huge KBs.
MAX_RESULTS = 500


def list_files_impl(
    kb_root: Path,
    subdir: str = "",
    kind_filter: str | None = None,
    limit: int = 100,
) -> str:
    """List md files under kb_root/subdir.

    Args:
        kb_root: Repo root.
        subdir: Optional subdir (e.g. "papers", "topics/attention").
                Empty = whole KB.
        kind_filter: If set, only show md whose frontmatter has kind=<this>.
                     Reading frontmatter is ~5x slower; omit for speed.
        limit: Max rows to return (capped at MAX_RESULTS).

    Returns:
        Human-readable multi-line string.
    """
    if subdir:
        try:
            base = safe_resolve(kb_root, subdir)
        except PathError as e:
            return f"[error] {e}"
    else:
        base = kb_root.resolve()

    if not base.exists():
        return f"[error] directory not found: {subdir or '.'}"
    if not base.is_dir():
        return f"[error] not a directory: {subdir}"

    limit = min(max(1, limit), MAX_RESULTS)

    rows: list[str] = []
    truncated = False
    total_found = 0

    for path in sorted(base.rglob("*.md")):
        # Skip anything under a dot-directory (.kb, .git, .venv, etc.).
        rel = path.resolve().relative_to(kb_root.resolve())
        if any(part.startswith(".") for part in rel.parts):
            continue

        if kind_filter is not None:
            try:
                post = frontmatter.load(path)
                actual_kind = post.metadata.get("kind")
                # v27: accept the legacy alias when filtering by
                # "note". Old mds (pre-v27) carry
                # "zotero_standalone_note"; new ones carry "note".
                # Filtering by "note" should find both so users
                # don't see half their notes until they re-import
                # the KB.
                if kind_filter == "note":
                    if actual_kind not in ("note", "zotero_standalone_note"):
                        continue
                elif actual_kind != kind_filter:
                    continue
            except Exception:
                continue

        total_found += 1
        if len(rows) >= limit:
            truncated = True
            continue

        size_kb = path.stat().st_size / 1024.0
        rows.append(f"{rel.as_posix()}  ({size_kb:.1f} KB)")

    if not rows:
        return f"No md files under {subdir or 'KB root'}."

    header_parts = [f"{len(rows)} file(s) under {subdir or 'KB root'}"]
    if kind_filter:
        header_parts.append(f"kind={kind_filter}")
    header = " · ".join(header_parts) + ":\n"

    out = header + "\n".join(rows)
    if truncated:
        out += f"\n\n[truncated: {total_found - len(rows)} more results]"
    return out
