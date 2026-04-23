"""AI-zone marker parsing for papers and notes.

The AI zone is the narrow slice of a paper/note md where agents can
write freely without kb-importer clobbering their edits. It's
delimited by explicit HTML-style markers:

    <!-- kb-ai-zone-start -->
    ...content...
    <!-- kb-ai-zone-end -->

This module locates those markers, extracts the zone body, and
rewrites a whole md with a new zone body — preserving everything
outside the zone verbatim.

Separate module so the regex / parsing lives in one place and can
be unit-tested without SQLite or git.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


AI_ZONE_START = "<!-- kb-ai-zone-start -->"
AI_ZONE_END = "<!-- kb-ai-zone-end -->"

# Tolerant regex: allow any whitespace around the markers. DOTALL so
# body may span newlines. Non-greedy so we match the first valid pair.
_ZONE_RE = re.compile(
    re.escape(AI_ZONE_START) + r"\s*(.*?)\s*" + re.escape(AI_ZONE_END),
    flags=re.DOTALL,
)


class ZoneError(Exception):
    """Raised when AI zone markers are missing, duplicated, or
    malformed in an md."""


@dataclass(frozen=True)
class ZoneLocation:
    """Where in the source md the zone lives. Byte-indexed for use
    with slice-and-splice rewrites.

    `start` is the index of the first char of AI_ZONE_START.
    `end`   is one past the last char of AI_ZONE_END (inclusive of
            the end marker).
    `body`  is the current content between the markers, whitespace
            trimmed.
    """
    start: int
    end: int
    body: str


def find_zone(md_text: str) -> ZoneLocation:
    """Locate the AI zone in a full md document.

    Raises ZoneError if:
      - start marker is missing
      - end marker is missing
      - markers are duplicated (ambiguous)
      - markers are in the wrong order

    Does NOT raise if the zone is empty — an empty zone is a valid
    state (new md, no content yet).
    """
    # Count occurrences to catch duplication.
    n_start = md_text.count(AI_ZONE_START)
    n_end = md_text.count(AI_ZONE_END)
    if n_start == 0 and n_end == 0:
        raise ZoneError(
            "AI zone markers not found. Expected "
            f"{AI_ZONE_START!r} and {AI_ZONE_END!r}."
        )
    if n_start == 0:
        raise ZoneError(f"start marker missing: {AI_ZONE_START!r}")
    if n_end == 0:
        raise ZoneError(f"end marker missing: {AI_ZONE_END!r}")
    if n_start > 1:
        raise ZoneError(
            f"start marker appears {n_start} times; must be exactly 1."
        )
    if n_end > 1:
        raise ZoneError(
            f"end marker appears {n_end} times; must be exactly 1."
        )

    start_idx = md_text.index(AI_ZONE_START)
    end_idx_of_end = md_text.index(AI_ZONE_END)
    if end_idx_of_end < start_idx:
        raise ZoneError("end marker appears before start marker.")

    # Match via regex to also capture the body.
    m = _ZONE_RE.search(md_text)
    if not m:
        # Shouldn't reach here given the count checks, but defensive.
        raise ZoneError("markers present but unmatched by zone regex.")

    body = m.group(1)
    # end index = position of AI_ZONE_END + its length.
    end_inclusive = end_idx_of_end + len(AI_ZONE_END)
    return ZoneLocation(start=start_idx, end=end_inclusive, body=body)


def replace_zone(md_text: str, new_body: str) -> str:
    """Return a copy of md_text with the AI zone body replaced.

    The markers themselves are preserved verbatim (they stay at
    their original byte positions relative to the rest of the file).
    `new_body` is inserted exactly between them with one blank line
    of padding on each side, for readability:

        <!-- kb-ai-zone-start -->
        <new_body here>
        <!-- kb-ai-zone-end -->

    If new_body is empty, the zone becomes minimally empty:
        <!-- kb-ai-zone-start -->
        <!-- kb-ai-zone-end -->
    """
    loc = find_zone(md_text)
    new_body = new_body.strip("\n")
    if new_body:
        replacement = (
            AI_ZONE_START + "\n\n" + new_body + "\n\n" + AI_ZONE_END
        )
    else:
        replacement = AI_ZONE_START + "\n" + AI_ZONE_END
    return md_text[:loc.start] + replacement + md_text[loc.end:]


def ensure_zone(md_text: str) -> str:
    """If the md lacks AI zone markers, append them at the end. If it
    already has valid markers, return the text unchanged. Raises
    ZoneError for partial/malformed states (don't guess repairs
    unless the user asked via `kb-write doctor --fix`).
    """
    n_start = md_text.count(AI_ZONE_START)
    n_end = md_text.count(AI_ZONE_END)
    if n_start == 0 and n_end == 0:
        # Completely missing → safe to append.
        tail = "\n" if not md_text.endswith("\n") else ""
        return (
            md_text + tail + "\n" +
            AI_ZONE_START + "\n" +
            AI_ZONE_END + "\n"
        )
    # Any other state goes through find_zone which raises on partial.
    find_zone(md_text)
    return md_text
