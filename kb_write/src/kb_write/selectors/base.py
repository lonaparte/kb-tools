"""Selector protocol for `kb-write re-read`.

Design: re-read needs to choose N papers from a candidate pool.
"How to choose" is open-ended (random, unread-first, stale-first,
related-to-recent, by-tag, ...). Hard-coding every strategy into
the re-read command would inflate it and discourage experimentation,
so we use a pluggable selector architecture.

Each selector is a class with two attributes (`name`, `description`)
and a `select(...)` method. Register in `selectors.registry.REGISTRY`.

Adding a new selector later = add one file + one line in registry.
No changes to re-read itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PaperInfo:
    """A paper candidate. The selector sees these; it doesn't have to
    open the md file. Fields are what's cheap to gather from
    frontmatter + file stat.

    paper_key:           md stem (incl. "-chNN" suffix for book chapters)
    md_path:             kb-root-relative POSIX path (e.g. "papers/ABCD1234.md")
    md_mtime:            filesystem mtime of the md (float seconds)
    fulltext_processed:  frontmatter flag (True/False; None if unknown)
    zotero_attachment_keys: list from frontmatter (empty if none)
    kb_tags:             list[str] from frontmatter (empty if missing)
    year:                int or None
    title:               str or None
    item_type:           frontmatter string (e.g. "journalArticle",
                         "book", "book_chapter")
    """
    paper_key: str
    md_path: str
    md_mtime: float
    fulltext_processed: bool | None = None
    zotero_attachment_keys: tuple[str, ...] = ()
    kb_tags: tuple[str, ...] = ()
    year: int | None = None
    title: str | None = None
    item_type: str | None = None


@runtime_checkable
class Selector(Protocol):
    """Pluggable strategy for picking N papers from a candidate pool.

    Attributes:
        name: short slug exposed via --selector on the CLI
              (e.g. "random", "unread-first").
        description: one-line help text for --help output.

    Method:
        select(candidates, *, count, kb_root, seed=None, **kwargs)
            → list of chosen paper_keys (length ≤ count).

    `**kwargs` catches selector-specific options passed via
    `--selector-arg key=value` on the CLI. Unknown kwargs a selector
    doesn't understand should be silently ignored — this keeps
    `--selector-arg` compositional across selectors that share some
    but not all options.
    """
    name: str
    description: str

    def select(
        self,
        candidates: list[PaperInfo],
        *,
        count: int,
        kb_root: Path,
        seed: int | None = None,
        **kwargs,
    ) -> list[str]:
        ...


def parse_selector_args(pairs: list[str]) -> dict:
    """Parse `--selector-arg key=value` pairs into a kwargs dict.

    Simple parser — no quoting, no nested lists. Values are strings.
    Selectors that need typed values (ints, lists) should coerce in
    their own `select()` from the kwargs dict. This keeps the CLI
    surface stable while selectors evolve.

    Repeated keys overwrite (last wins).
    """
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(
                f"--selector-arg {pair!r}: expected key=value"
            )
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out
