"""Tool: grep_md.

Case-insensitive substring search over KB md files. Not regex — we
intentionally keep it simple: "did the word X appear in my KB?"

This is a quick-lane tool for exact/literal matches. Semantic
similarity is the job of deep_search (not in this phase).
"""
from __future__ import annotations

from pathlib import Path

from ..paths import PathError, safe_resolve


# Per-file byte limit — files larger than this are skipped. Large files
# are almost always not text we'd want to match against anyway.
MAX_FILE_BYTES = 1_000_000
# Max matching files returned.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# Max hit lines per file.
MAX_HITS_PER_FILE = 3
# Max chars shown per hit line.
HIT_LINE_MAX_CHARS = 240


def grep_md_impl(
    kb_root: Path,
    pattern: str,
    scope: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Search for `pattern` (case-insensitive, literal, multi-term AND)
    across md files in the given scope.

    Args:
        pattern: Space-separated terms; all must appear in the file.
        scope: Subdirs to search (e.g. ["papers", "topics"]). Empty or
               None = whole KB.
        limit: Max matching files to return.

    Returns:
        Human-readable multi-line text: each matched file plus up to
        MAX_HITS_PER_FILE excerpt lines.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return "[error] empty pattern"

    terms = [t.lower() for t in pattern.split() if t.strip()]
    if not terms:
        return "[error] no search terms after stripping"

    limit = min(max(1, limit), MAX_LIMIT)

    # Build list of search roots.
    roots: list[Path] = []
    if scope:
        for s in scope:
            try:
                p = safe_resolve(kb_root, s)
            except PathError as e:
                return f"[error] bad scope {s!r}: {e}"
            if p.is_dir():
                roots.append(p)
    if not roots:
        roots.append(kb_root.resolve())

    hits: list[tuple[str, list[str]]] = []  # (rel_path, excerpt_lines)
    scanned = 0
    truncated = False

    for root in roots:
        for path in sorted(root.rglob("*.md")):
            # Skip hidden dirs.
            try:
                rel = path.resolve().relative_to(kb_root.resolve())
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue

            scanned += 1
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            lowered = text.lower()
            if not all(t in lowered for t in terms):
                continue

            excerpts = _find_excerpts(text, terms, MAX_HITS_PER_FILE)
            hits.append((rel.as_posix(), excerpts))
            if len(hits) >= limit:
                truncated = True
                break
        if truncated:
            break

    if not hits:
        return f"No matches for {pattern!r} (scanned {scanned} files)."

    out = [f"{len(hits)} file(s) match {pattern!r}:", ""]
    for rel, excerpts in hits:
        out.append(f"📄 {rel}")
        for ex in excerpts:
            out.append(f"  {ex}")
        out.append("")

    if truncated:
        out.append(f"[stopped at limit={limit}; use a smaller scope or more terms]")
    return "\n".join(out).rstrip() + "\n"


def _find_excerpts(text: str, terms: list[str], max_hits: int) -> list[str]:
    """Return up to `max_hits` short excerpt lines (with line numbers)
    containing any of the terms.
    """
    excerpts: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        ll = line.lower()
        if any(t in ll for t in terms):
            shown = line.strip()
            if len(shown) > HIT_LINE_MAX_CHARS:
                shown = shown[: HIT_LINE_MAX_CHARS - 3] + "..."
            excerpts.append(f"L{lineno}: {shown}")
            if len(excerpts) >= max_hits:
                break
    return excerpts
