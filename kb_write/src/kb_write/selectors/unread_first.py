"""UnreadFirstSelector — papers never re-summarized before, preferred.

Default selector. For users running `kb-write re-read` periodically,
this biases toward papers the agent hasn't revisited yet, letting
the backlog shrink with each run. Once every paper has been
re-read at least once, the selector falls back to random sampling
over the whole pool.

"Re-read history" comes from `<kb_root>/.kb-mcp/events.jsonl`:
events with event_type=re_read whose category indicates the run
actually executed (success OR a non-trivial skip like llm_error,
but NOT dryrun_selected). The selector is defensive — if the events
log is missing or malformed, it degrades to random.
"""
from __future__ import annotations

import random
from pathlib import Path

from .base import PaperInfo


class UnreadFirstSelector:
    name = "unread-first"
    description = (
        "Papers never re-summarized before preferred; fall back to "
        "random sample from the whole pool if fewer than `count` "
        "unread papers remain."
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
        already_read = _load_read_set(kb_root)

        unread = [c for c in candidates if c.paper_key not in already_read]

        if len(unread) >= count:
            # Plenty of unread — pick from them.
            return [c.paper_key for c in rng.sample(unread, count)]

        # Not enough unread: take all unread, top up from the rest
        # randomly. This keeps the "re-read the oldest stuff first"
        # invariant but doesn't leave the user short of picks.
        chosen_keys = [c.paper_key for c in unread]
        remaining = [c for c in candidates if c.paper_key not in set(chosen_keys)]
        extra_needed = count - len(chosen_keys)
        if remaining and extra_needed > 0:
            extras = rng.sample(
                remaining, min(extra_needed, len(remaining))
            )
            chosen_keys.extend(c.paper_key for c in extras)
        return chosen_keys


def _load_read_set(kb_root: Path) -> set[str]:
    """Return paper_keys that appear in an executed re_read event.

    We include: RE_READ_SUCCESS, RE_READ_SKIP_MTIME, RE_READ_SKIP_LLM,
    RE_READ_SKIP_PDF, RE_READ_SKIP_NOT_PROCESSED, RE_READ_SKIP_BAD_TARGET
    (all indicate a real attempt was made — even if it failed, we
    don't want to re-try the same paper on the very next run).

    We EXCLUDE: RE_READ_DRYRUN (paper was chosen but not processed).

    Defensive: any import/read error returns an empty set (degrade to
    treating nothing as read → random fallback kicks in).

    Forward-compat note: any newly-added RE_READ_SKIP_* category is
    considered an "attempt was made" unless explicitly documented
    otherwise. Re-entering this function for a new category only
    needs the constant added to the imports + the `executed_cats`
    set below; no downstream changes.
    """
    try:
        from kb_importer.events import (
            read_events, EVENT_RE_READ,
            RE_READ_SUCCESS, RE_READ_SKIP_MTIME,
            RE_READ_SKIP_LLM, RE_READ_SKIP_PDF,
            RE_READ_SKIP_NOT_PROCESSED,
            RE_READ_SKIP_BAD_TARGET,
        )
    except ImportError:
        return set()
    try:
        events = read_events(kb_root, event_types=[EVENT_RE_READ])
    except Exception:
        return set()
    executed_cats = {
        RE_READ_SUCCESS, RE_READ_SKIP_MTIME,
        RE_READ_SKIP_LLM, RE_READ_SKIP_PDF,
        RE_READ_SKIP_NOT_PROCESSED,
        RE_READ_SKIP_BAD_TARGET,
    }
    out: set[str] = set()
    for e in events:
        if e.get("category") in executed_cats:
            pk = e.get("paper_key")
            if pk:
                out.add(pk)
    return out
