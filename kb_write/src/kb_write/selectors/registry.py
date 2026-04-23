"""Selector registry.

Single source of truth for "which selectors does `kb-write re-read`
expose by default?". Adding a new selector = one file + one line
here.

Order here doesn't affect CLI behaviour — selection is by name —
but it's what `kb-write re-read --list-selectors` prints, so
default selector first, then roughly "easy → complex".
"""
from __future__ import annotations

from .base import Selector
from .unread_first import UnreadFirstSelector
from .random_sel import RandomSelector
from .stale_first import StaleFirstSelector
from .never_summarized import NeverSummarizedSelector
from .oldest_summary import OldestSummaryFirstSelector
from .by_tag import ByTagSelector
from .related_to_recent import RelatedToRecentSelector


REGISTRY: dict[str, Selector] = {
    s.name: s for s in (
        UnreadFirstSelector(),
        RandomSelector(),
        StaleFirstSelector(),
        NeverSummarizedSelector(),
        OldestSummaryFirstSelector(),
        ByTagSelector(),
        RelatedToRecentSelector(),
    )
}

DEFAULT_SELECTOR_NAME = "unread-first"


def describe_all() -> str:
    """Formatted string listing all registered selectors. Used by
    `--list-selectors`.
    """
    rows = [
        f"Available selectors (default: {DEFAULT_SELECTOR_NAME}):", "",
    ]
    for name, sel in REGISTRY.items():
        marker = " (default)" if name == DEFAULT_SELECTOR_NAME else ""
        rows.append(f"  {name}{marker}")
        rows.append(f"      {sel.description}")
        rows.append("")
    return "\n".join(rows).rstrip()
