"""NeverSummarizedSelector — only papers without fulltext_processed.

For filling fulltext coverage gaps. Strictly filters out anything
that already has a summary. If count > available, returns everything
available (doesn't pad from summarized pool — that would defeat the
purpose).

⚠ Caveat when used with `kb-write re-read`:
   re_summarize() only operates on papers where fulltext_processed=true.
   Picking unprocessed papers into re-read will cause every single
   one to fail with a `skip_not_processed` event. This is reported
   clearly in `kb-mcp report`, but the selector is really aimed at
   other future operations (e.g. a batch `kb-importer --fulltext`
   dispatcher). For "generate initial summaries for what I haven't
   processed", use: `kb-importer import --fulltext --all-unprocessed`.
"""
from __future__ import annotations

import random
from pathlib import Path

from .base import PaperInfo


class NeverSummarizedSelector:
    name = "never-summarized"
    description = (
        "Only papers with fulltext_processed != true. No fallback "
        "to summarized papers — returns fewer than `count` if "
        "unsummarized pool is small."
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
        pool = [c for c in candidates if not c.fulltext_processed]
        if not pool:
            return []
        rng = random.Random(seed)
        n = min(count, len(pool))
        return [c.paper_key for c in rng.sample(pool, n)]
