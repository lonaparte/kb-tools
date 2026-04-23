"""Module-level helper functions for the indexer.

Extracted from indexer.py in v0.28.0 so that the submodules
(embedding_pass, stale_cleanup, link_resolve) can share them
without a circular import on `indexer`. indexer.py re-exports these
symbols for backward compatibility with anyone who imports them
from there (tests, external scripts).
"""
from __future__ import annotations

import json
import re
import struct
from datetime import datetime, timezone

from kb_core import FULLTEXT_START, FULLTEXT_END


_FULLTEXT_PATTERN = re.compile(
    re.escape(FULLTEXT_START) + r"(.*?)" + re.escape(FULLTEXT_END),
    flags=re.DOTALL,
)


def _extract_fulltext_body(content: str) -> str:
    m = _FULLTEXT_PATTERN.search(content)
    if not m:
        return ""
    body = m.group(1).strip()
    # Treat the placeholder comment as empty.
    if "Empty when fulltext_processed=false" in body:
        return ""
    return body


def _extract_abstract(content: str) -> str:
    """Pull abstract text between '## Abstract' heading and next '##'.

    Loose heuristic: if there's no '## Abstract' heading, returns "".
    """
    m = re.search(
        r"##\s+Abstract\s*\n(.*?)(?=\n##\s+|\Z)",
        content,
        flags=re.DOTALL,
    )
    if not m:
        return ""
    # Strip the zotero-field marker comment kb-importer leaves.
    text = re.sub(r"<!--\s*zotero-field:.*?-->", "", m.group(1)).strip()
    return text


def _safe_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Match headings of the form "## 1. <title>" through "## 7. <title>"
# in the Chinese-language section summary format (see
# kb_importer/templates/ai_summary_prompt.md). The number lets us
# attach section_num; content extends to the next "^## N." line or
# end of text. DOTALL so .*? can span newlines.
_SECTION_RE = re.compile(
    r"^##\s+(\d+)\.\s*(.+?)\n(.*?)(?=^##\s+\d+\.\s|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)


def _split_fulltext_sections(text: str) -> list[tuple[int, str, str]]:
    """Return [(num, title, body), ...] from "## N. ..." headings.

    Empty list if no matches (caller should treat as single chunk).
    Title is trimmed; body has whitespace stripped on both ends.
    """
    out: list[tuple[int, str, str]] = []
    for m in _SECTION_RE.finditer(text):
        num = int(m.group(1))
        title = m.group(2).strip()
        body = m.group(3).strip()
        if body:
            out.append((num, title, body))
    return out


def _strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block if present."""
    if not content.startswith("---\n"):
        return content
    # Find the closing ---
    end = content.find("\n---\n", 4)
    if end < 0:
        return content
    return content[end + 5:]


def _clamp(text: str, max_chars: int = 6000) -> str:
    """Hard cap on chunk size to stay under embedding model token limit."""
    if len(text) <= max_chars:
        return text
    # Cut at a paragraph boundary near the limit when possible.
    cut = text.rfind("\n\n", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    return text[:cut] + "\n\n…[truncated]"


def _authors_flat(authors_json: str | None) -> str:
    """Turn the JSON array stored in papers.authors into a flat string."""
    if not authors_json:
        return ""
    try:
        parsed = json.loads(authors_json)
        if isinstance(parsed, list):
            return ", ".join(str(a) for a in parsed if a)
    except Exception:
        pass
    return ""


def _vec_blob(vec: list[float]) -> bytes:
    """Serialize a float vector for sqlite-vec.

    vec0 accepts either a list (via sqlite-vec's type coercion) or a
    packed bytes blob. We use struct-packed float32 for portability
    and size (4 bytes × dim vs. Python list overhead). This matches
    the format documented for sqlite-vec.
    """
    return struct.pack(f"{len(vec)}f", *vec)
