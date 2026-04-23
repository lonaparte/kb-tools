"""RandomSelector — pure uniform random sample.

The simplest selector. Useful as a baseline and as a fallback when
other selectors don't have enough candidates.
"""
from __future__ import annotations

import random
from pathlib import Path

from .base import PaperInfo


class RandomSelector:
    name = "random"
    description = "Uniform random sample from the candidate pool."
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
        n = min(count, len(candidates))
        chosen = rng.sample(candidates, n)
        return [c.paper_key for c in chosen]
