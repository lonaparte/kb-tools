"""ByTagSelector — only papers carrying a given kb_tag.

Useful when you want to re-read a thematic subset — e.g.
--selector-arg tag=foundational to re-read all your "foundational"
marked papers, or tags=to-review,revisit for a combined backlog.

CLI usage:
    kb-write re-read --selector by-tag --selector-arg tag=foundational
    kb-write re-read --selector by-tag --selector-arg tags=review,redo

Matching is case-insensitive. Papers are accepted if any of their
kb_tags matches any of the requested tags (OR semantics).
"""
from __future__ import annotations

import random
from pathlib import Path

from .base import PaperInfo


class ByTagSelector:
    name = "by-tag"
    description = (
        "Only papers with the specified kb_tag(s). Requires "
        "--selector-arg tag=<tagname> or --selector-arg tags=a,b,c "
        "(OR semantics). Case-insensitive. Order is random."
    )
    ACCEPTED_KWARGS = frozenset({"tag", "tags"})

    def select(
        self,
        candidates: list[PaperInfo],
        *,
        count: int,
        kb_root: Path,
        seed: int | None = None,
        **kwargs,
    ) -> list[str]:
        # Accept either `tag=name` (single) or `tags=a,b,c` (list).
        # Both normalise to a lowercased set for case-insensitive
        # matching — users often type "review" while frontmatter has
        # "Review", and vice versa.
        requested: set[str] = set()
        single = kwargs.get("tag")
        multi = kwargs.get("tags")
        if single:
            requested.add(single.strip().lower())
        if multi:
            for t in multi.split(","):
                t = t.strip()
                if t:
                    requested.add(t.lower())
        if not requested:
            raise ValueError(
                "by-tag selector requires --selector-arg tag=<tagname> "
                "or --selector-arg tags=a,b,c"
            )

        pool = [
            c for c in candidates
            if any(
                (kb_t or "").strip().lower() in requested
                for kb_t in c.kb_tags
            )
        ]
        if not pool:
            return []
        rng = random.Random(seed)
        n = min(count, len(pool))
        return [c.paper_key for c in rng.sample(pool, n)]
