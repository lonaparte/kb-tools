"""StaleFirstSelector — oldest md_mtime first.

Useful when you suspect your oldest imports have outdated
summaries (models improved, prompts updated) and you want to
refresh them first. Tie-break is random (seeded).
"""
from __future__ import annotations

import random
from pathlib import Path

from .base import PaperInfo


class StaleFirstSelector:
    name = "stale-first"
    description = "Papers with the oldest md_mtime first (tie-break random)."
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
        # Sort by mtime asc; random tie-break via (mtime, rng.random()).
        # This preserves "oldest first" semantics while scrambling
        # identical-mtime runs of papers (which happens after
        # mass-import — whole batches share a second).
        decorated = [(c.md_mtime, rng.random(), c) for c in candidates]
        decorated.sort()
        n = min(count, len(candidates))
        return [c.paper_key for _, _, c in decorated[:n]]
