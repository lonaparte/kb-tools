"""Where different Zotero item types go in the fulltext pipeline.

Single source of truth for "which Zotero item_type takes which
summary route". Before this module existed, summary_cmd had its own
NO_FULLTEXT_ITEM_TYPES set and import_cmd's --fulltext pass had no
eligibility check at all — so it would cheerfully feed a 500-page
book to the 7-section summarizer and get garbage back. Centralising
here lets every entry point agree on the routing.

Three modes:

- "short"  – the standard 7-section summary pipeline. Designed for
             journal articles and other "abstract + method + result"
             papers. Current short-pipeline implementation truncates
             input at 200K chars which is plenty for articles but
             disastrous for books.
- "long"   – the long-form pipeline (chapter splitting, per-chapter
             thought generation). Designed for books and theses
             where a single global summary loses too much. See
             `kb_importer.longform`.
- "none"   – neither pipeline is a fit. Web pages, emails, standalone
             notes. --fulltext skips these silently (reported as
             `skipped_ineligible` in the final counts).

Rationale for the split:
- journalArticle, conferencePaper, preprint → short
- book, bookSection, thesis → long (too long for a single pass)
- report sits awkwardly: grey literature reports can be short
  (a 10-page white paper) or long (a 200-page government study).
  Default to "long" to be safe; users can force with --no-longform.
- webpage, blogPost, email, note → none
"""
from __future__ import annotations

from typing import Literal


FulltextMode = Literal["short", "long", "none"]


# --- exact sets (all lowercase, matching Zotero itemType strings) ---

SHORT_PIPELINE_TYPES = frozenset({
    "journalArticle",
    "conferencePaper",
    "preprint",
    "manuscript",
    "magazineArticle",
    "newspaperArticle",
})

LONG_PIPELINE_TYPES = frozenset({
    "book",
    "bookSection",
    "thesis",
    "report",
})

NO_FULLTEXT_TYPES = frozenset({
    "webpage",
    "blogPost",
    "forumPost",
    "email",
    "note",
    "attachment",
    "document",  # generic Zotero "document" with no more info
    "letter",
    "interview",
    "audioRecording",
    "videoRecording",
    "podcast",
    "radioBroadcast",
    "tvBroadcast",
    "film",
    "presentation",
    "case",
    "statute",
    "bill",
    "hearing",
    "patent",
    "map",
    "artwork",
    "computerProgram",
    "dataset",
    "software",
    "standard",
})


def fulltext_mode(item_type: str | None) -> FulltextMode:
    """Route a Zotero itemType to one of short / long / none.

    Unknown types default to "short" — the conservative choice for a
    type we haven't classified: the worst case is a truncated summary,
    which is recoverable (user can run `--force-fulltext --longform`),
    versus routing something to "none" and having it silently never
    get a summary at all.
    """
    if not item_type:
        return "short"
    t = str(item_type).strip()
    if t in LONG_PIPELINE_TYPES:
        return "long"
    if t in NO_FULLTEXT_TYPES:
        return "none"
    return "short"


# Backwards-compatibility alias for existing call sites that only
# cared about the binary "eligible / not eligible" question before
# the long-pipeline split was introduced. New code should call
# fulltext_mode() directly so it can distinguish short vs long.
def is_fulltext_eligible(item_type: str | None) -> bool:
    """True iff any fulltext pipeline (short or long) handles this type.

    Kept for compatibility with kb_importer.commands.summary_cmd's
    original `_is_fulltext_eligible`. Equivalent to
    `fulltext_mode(item_type) != "none"`.
    """
    return fulltext_mode(item_type) != "none"
