"""OldestSummaryFirstSelector — oldest fulltext_extracted_at first.

Papers whose summary was generated longest ago are re-read first.
Distinguishes from stale-first (which uses md_mtime, a file system
signal that advances on any edit — including the agent just
appending a thought). oldest-summary-first uses the frontmatter
field `fulltext_extracted_at` so the ordering reflects *when the
LLM actually wrote the summary*, not when the file was last
touched.

Papers without `fulltext_extracted_at` (never summarized), or
whose value fails to parse as a datetime, are sorted first — they
are treated as "infinitely old" so they get picked before any
known-old summarized ones.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from pathlib import Path

from .base import PaperInfo


class OldestSummaryFirstSelector:
    name = "oldest-summary-first"
    description = (
        "Oldest fulltext_extracted_at first (from frontmatter). "
        "Never-summarized papers sorted before all summarized ones. "
        "Tie-break random."
    )
    ACCEPTED_KWARGS = frozenset()

    def select(
        self,
        candidates: list[PaperInfo],
        *,
        count: int,
        kb_root: Path,
        seed: int | None = None,
        **_kwargs,
    ) -> list[str]:
        if not candidates:
            return []
        rng = random.Random(seed)
        # Decorate with parsed-timestamp; fall back to "very old"
        # sentinel for missing / malformed values so they sort first.
        # Using a timezone-aware datetime.min avoids mixing naive and
        # aware comparators at sort time (Python raises TypeError on
        # cross-awareness compare).
        sentinel_old = datetime.min.replace(tzinfo=timezone.utc)
        decorated = []
        for c in candidates:
            raw = _read_extracted_at(kb_root, c.md_path)
            ts = _parse_ts(raw) or sentinel_old
            decorated.append((ts, rng.random(), c))
        decorated.sort(key=lambda t: (t[0], t[1]))
        n = min(count, len(candidates))
        return [c.paper_key for _, _, c in decorated[:n]]


_EXTRACTED_AT_RE = re.compile(
    r'^fulltext_extracted_at:\s*["\']?([^"\'\n]+)["\']?\s*$',
    re.MULTILINE,
)


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse a frontmatter timestamp robustly. Accepts:

    - RFC 3339 with Z suffix: "2024-01-15T10:00:00Z"
    - RFC 3339 with offset:   "2024-01-15T10:00:00+00:00"
    - Bare date:              "2024-01-15"
    - Naive datetime:         "2024-01-15T10:00:00" (assumed UTC)

    Returns None for unparseable / missing. Caller treats None as
    "extremely old" so malformed entries don't pollute the ordering.
    """
    if not raw:
        return None
    s = raw.strip()
    # Normalise Z → +00:00 for fromisoformat() (which doesn't
    # accept Z on Python < 3.11 and is finicky even on 3.11+).
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Last-resort: bare date → midnight UTC
        try:
            dt = datetime.strptime(raw.strip()[:10], "%Y-%m-%d")
        except ValueError:
            return None
    # Ensure tz-aware so comparisons are total.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_extracted_at(kb_root: Path, md_rel: str) -> str | None:
    """Peek at frontmatter for fulltext_extracted_at without
    importing frontmatter (to keep selector hot-path cheap).

    Reads only the top ~4KB of the file — frontmatter is always
    right after the opening `---\\n`. If the field isn't there or
    the md doesn't have a frontmatter block, returns None.
    """
    p = kb_root / md_rel
    try:
        with open(p, "r", encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return None
    if not head.startswith("---\n"):
        return None
    end = head.find("\n---\n", 4)
    if end < 0:
        return None
    frontmatter = head[4:end]
    m = _EXTRACTED_AT_RE.search(frontmatter)
    return m.group(1).strip() if m else None
