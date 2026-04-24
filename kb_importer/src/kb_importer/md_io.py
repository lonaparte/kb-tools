"""Markdown file I/O with AI-zone preservation.

Core contract (from spec §4): when rewriting a paper/note md, we MUST
preserve:
  1. All frontmatter fields starting with `kb_`.
  2. The content between `<!-- kb-ai-zone-start -->` and
     `<!-- kb-ai-zone-end -->`.
  3. The content between `<!-- kb-fulltext-start -->` and
     `<!-- kb-fulltext-end -->` (in metadata-only mode).

Everything else is owned by kb-importer and gets overwritten.

This module is deliberately free of any Zotero knowledge — it only cares
about the md format.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter


log = logging.getLogger(__name__)


# v0.28.0: marker constants now come from their canonical source
# (kb_core for fulltext, kb_write.zones for ai-zone). Earlier
# versions re-declared the literals here as well, and
# check_package_consistency ran a substring parity check between
# this file, kb_write.ops.re_summarize, and kb_mcp.indexer to
# ensure they didn't drift. The consolidation makes the parity
# check trivially true (same import source) and removes the
# drift risk altogether. Kept re-exported so existing callers
# `from kb_importer.md_io import FULLTEXT_START` still work.
from kb_core import FULLTEXT_START, FULLTEXT_END
from kb_write.zones import AI_ZONE_START, AI_ZONE_END

# Default placeholder shown when the AI zone is empty.
AI_ZONE_PLACEHOLDER = (
    "<!-- This region is owned by the AI layer via MCP. "
    "kb-importer never touches it. -->"
)
FULLTEXT_PLACEHOLDER = (
    "<!-- Empty when fulltext_processed=false. "
    "Populated by --fulltext mode. -->"
)


@dataclass
class PreservedContent:
    """What we lift from an existing md before rewriting it.

    Any of these may be None/empty if the old file didn't have them
    (e.g. first-time import, or the file is missing the section).
    """

    kb_frontmatter: dict[str, Any] = field(default_factory=dict)
    # fulltext_* frontmatter fields (fulltext_processed, fulltext_source,
    # fulltext_source_note_keys, fulltext_processed_at, fulltext_model).
    # These are owned by kb-importer (not the AI layer), but are
    # preserved across re-renders because the fulltext region itself
    # is preserved — so rewriting them to defaults would lie about the
    # state of the file.
    fulltext_frontmatter: dict[str, Any] = field(default_factory=dict)
    # User-editable frontmatter fields that kb-importer normally
    # regenerates, but honours if the user overrode them. Currently
    # just `zotero_main_attachment_key` (our heuristic might pick
    # wrong; user can correct). Only survives if the override still
    # points at a valid attachment.
    user_override_fields: dict[str, Any] = field(default_factory=dict)
    ai_zone_body: str | None = None
    fulltext_body: str | None = None
    # 1.3.0: Revisits region accumulates re-read results. kb-importer
    # must preserve this across re-imports / `sync` / `--force-fulltext`
    # — without this field, a sync of a paper that had revisits would
    # silently drop the entire Revisits section. `section_markdown`
    # is the full region text INCLUDING the surrounding `## Revisits`
    # heading and the kb-revisits-{start,end} markers (so
    # inject_preserved can round-trip it unchanged).
    revisits_section: str | None = None


def read_md(path: Path) -> frontmatter.Post:
    """Parse a markdown file with YAML frontmatter.

    Raises FileNotFoundError if the file doesn't exist.
    """
    with open(path, "r", encoding="utf-8") as f:
        return frontmatter.load(f)


def extract_preserved(path: Path) -> PreservedContent:
    """Extract kb_* and fulltext_* frontmatter fields, plus region
    contents, from an existing md. Returns empty PreservedContent if
    file doesn't exist.
    """
    if not path.exists():
        return PreservedContent()

    post = read_md(path)

    kb_fields = {
        k: v for k, v in post.metadata.items() if k.startswith("kb_")
    }
    # fulltext_* fields track the state of the fulltext region. Since
    # the region content itself is preserved across re-renders, these
    # metadata fields must be preserved too — otherwise a re-render
    # would reset fulltext_processed back to False while the region
    # still holds a valid summary.
    fulltext_fields = {
        k: v for k, v in post.metadata.items() if k.startswith("fulltext_")
    }

    # User-override fields: certain fields we regenerate but honour
    # if the user corrected them.
    _OVERRIDABLE = ("zotero_main_attachment_key",)
    user_override_fields = {
        k: post.metadata[k]
        for k in _OVERRIDABLE
        if k in post.metadata and post.metadata[k] is not None
    }

    ai_zone = _extract_between(post.content, AI_ZONE_START, AI_ZONE_END)
    fulltext = _extract_between(post.content, FULLTEXT_START, FULLTEXT_END)

    # Treat default placeholders as "empty" so we don't preserve them
    # into new files where they'd be regenerated anyway.
    if ai_zone and AI_ZONE_PLACEHOLDER in ai_zone and len(ai_zone.strip()) < 200:
        ai_zone = None
    if fulltext and FULLTEXT_PLACEHOLDER in fulltext and len(fulltext.strip()) < 200:
        fulltext = None

    # Data-loss fallback: an earlier version of writeback_summary
    # clobbered the fulltext markers while leaving
    # fulltext_processed=true. Subsequent re-imports would then find
    # no markers, return fulltext=None, and silently discard the
    # summary on write. Guard against that.
    #
    # v25: previously this branch grabbed `post.content` in its
    # entirety, which caused a different bug — sections outside the
    # real fulltext region (Abstract, Attachments, Zotero Notes)
    # were dragged into `fulltext_body`, then the next render
    # *regenerated* those sections from metadata AND re-wrapped the
    # preserved "body" (which still had the old copies) inside
    # kb-fulltext markers. Result: Abstract / Attachments appearing
    # twice in the md. Fix: extract only the range from the
    # `## AI Summary (from Full Text)` heading (the legacy
    # pre-v21 title for this section) to the next `\n---\n`
    # separator or end of file. This matches the actual fulltext
    # section's location and excludes Abstract/Attachments above
    # and the AI zone below. Harmless for correctly-structured mds
    # (markers found → fulltext already non-None → branch doesn't
    # run). If the heading isn't found either (truly unrecognisable
    # legacy format), we fall back to preserving the entire body
    # — better than silently losing content, even if it risks the
    # duplication bug. Log message distinguishes the two paths so
    # the user can inspect.
    if (
        fulltext is None
        and fulltext_fields.get("fulltext_processed")
        and post.content.strip()
        and FULLTEXT_START not in post.content
    ):
        fulltext = _extract_legacy_fulltext_by_heading(post.content)
        if fulltext is not None:
            log.warning(
                "%s: fulltext_processed=true but markers missing; "
                "recovered fulltext region from '## AI Summary "
                "(from Full Text)' heading (v25 heading-based "
                "self-heal).",
                path.name,
            )
        else:
            # No heading either — last-resort fallback to entire
            # body. This can double-insert Abstract/Attachments on
            # re-render; accept the risk since losing the summary
            # entirely is worse. User should inspect the md after
            # re-import to check for duplication.
            fulltext = post.content
            log.warning(
                "%s: fulltext_processed=true, markers AND heading "
                "both missing; preserving ENTIRE body as fulltext "
                "region. Some sections may appear duplicated on "
                "re-render — inspect manually.",
                path.name,
            )

    # 1.3.0: preserve the Revisits section (if any) as opaque
    # verbatim text. md_builder doesn't know how to render it; we
    # just splice it back in at the end on re-render.
    revisits_section = _extract_revisits_section(post.content)

    return PreservedContent(
        kb_frontmatter=kb_fields,
        fulltext_frontmatter=fulltext_fields,
        user_override_fields=user_override_fields,
        ai_zone_body=ai_zone,
        fulltext_body=fulltext,
        revisits_section=revisits_section,
    )


# 1.3.0: markers that delimit the Revisits region.
_REVISITS_START_MARKER = "<!-- kb-revisits-start -->"
_REVISITS_END_MARKER   = "<!-- kb-revisits-end -->"


def _extract_revisits_section(body: str) -> str | None:
    """Return the verbatim `## Revisits` section including heading
    and markers, or None if absent. Used by extract_preserved to
    round-trip the region across kb-importer re-renders.

    We look for `## Revisits` as the heading, but the markers are
    what authoritatively bound the content (heading could be
    missing from a hand-edited file; markers must not).
    """
    start = body.find(_REVISITS_START_MARKER)
    if start < 0:
        return None
    end = body.find(_REVISITS_END_MARKER, start + len(_REVISITS_START_MARKER))
    if end < 0:
        # Corrupt region — let doctor flag it, don't silently drop.
        return None
    end_full = end + len(_REVISITS_END_MARKER)

    # Walk backwards from start to pick up the `## Revisits` heading
    # and the blank line(s) above the start marker, so the round-trip
    # preserves the original layout. Cap the walk at 200 chars so a
    # malformed / heading-missing file still round-trips (we accept
    # whatever's between nearest `\n##` or start-of-body and the end
    # marker).
    search_from = max(0, start - 200)
    heading_idx = body.rfind("\n## Revisits", search_from, start)
    if heading_idx >= 0:
        section_begin = heading_idx + 1  # skip the leading '\n'
    else:
        section_begin = start  # no heading found; markers only
    return body[section_begin:end_full]


# Legacy title for the fulltext region (pre-v21). Exact string must
# match what md_builder writes so self-heal can find it. Kept as a
# module constant so tests and the extractor below agree.
_LEGACY_FULLTEXT_HEADING = "## AI Summary (from Full Text)"


def _extract_legacy_fulltext_by_heading(body: str) -> str | None:
    """Find the `## AI Summary (from Full Text)` heading in a legacy
    md body (pre-v21, no kb-fulltext markers) and return the content
    from immediately after the heading to either:

      - the `---\\n<!-- kb-ai-zone-start -->` sentinel that separates
        the fulltext region from the AI zone in the current
        template, or
      - end of file,

    whichever comes first.

    Returns None if the heading can't be found — caller then
    decides whether to fall back to "entire body" or just give up.

    Boundary choices:
      - Heading match: line-exact, case-sensitive. Trailing
        whitespace tolerated.
      - End sentinel: the `---` thematic break must be *immediately
        followed* by `<!-- kb-ai-zone-start -->` (or EOF). This is
        v27's replacement for the v25 boundary of "any bare `---`
        line", which over-matched: markdown horizontal rules and
        table separators inside the summary prose would trigger
        early cut-off, silently losing the rest of the summary.
        The AI-zone start marker is a unique opener so the
        combination `---\\n<!-- kb-ai-zone-start -->` is unambiguous.
      - The heading LINE ITSELF is not included in the returned
        string — md_builder regenerates the heading on next render.
    """
    lines = body.splitlines()
    # Find heading line.
    heading_idx = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == _LEGACY_FULLTEXT_HEADING:
            heading_idx = i
            break
    if heading_idx is None:
        return None

    # Find the end: a `---` line IMMEDIATELY followed by the AI-zone
    # start marker. Checking two lines (not one) avoids the v25 bug
    # where any horizontal rule inside the summary (e.g. a markdown
    # `---` between sections) would truncate and lose trailing
    # content.
    end_idx = len(lines)  # default: end of file
    for j in range(heading_idx + 1, len(lines) - 1):
        if (
            lines[j].strip() == "---"
            and lines[j + 1].strip() == AI_ZONE_START
        ):
            end_idx = j
            break

    # Extract body between heading+1 and end_idx.
    extracted = "\n".join(lines[heading_idx + 1:end_idx])
    # Strip leading/trailing blank lines but preserve internal blanks
    # (summary formatting often uses them for readability).
    return extracted.strip("\n")


def _extract_between(text: str, start_marker: str, end_marker: str) -> str | None:
    """Return the content strictly between two markers, or None if not found.

    The returned string does NOT include the markers themselves. Leading
    and trailing whitespace is preserved (we don't know if the AI relies
    on it).
    """
    # Escape markers for regex; use DOTALL so .* spans newlines.
    pattern = re.escape(start_marker) + r"(.*?)" + re.escape(end_marker)
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return None
    return m.group(1)


def atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically.

    Delegates to kb_write.atomic.atomic_write which is the single
    canonical implementation (O_EXCL guard, mtime checks, parent-dir
    fsync, the full set of durability features). Previously this file
    had its own simpler implementation that lacked those guards,
    producing two slightly-different "atomic" primitives in the same
    process — this wrapper preserves the minimal-args call site
    signature used by kb_importer code while funnelling all writes
    through the one implementation that's been reviewed and tested.
    """
    from kb_write.atomic import atomic_write as _write
    _write(path, content)


def peek_frontmatter(md_path: Path) -> dict[str, Any] | None:
    """Read and parse ONLY the frontmatter block of an md file.

    Streams line by line until the closing `---` delimiter of the
    YAML header, then runs yaml.safe_load on the collected header.
    On any error (missing file, no frontmatter, unparseable YAML,
    runaway file without closing `---`) returns None.

    Motivation: when scanning 1000+ papers to classify/filter, reading
    the full body into memory just to inspect a handful of metadata
    keys is 100-500x more IO than needed. Used by is_fulltext_processed
    and _peek_item_type (and any future "classify by frontmatter"
    scan). Unified here so all callers agree on:
      - the 500-line runaway cap (catches corrupt/half-written mds)
      - the fail-safe return None (never raises; callers branch on
        None to mean "can't tell" and fall back to conservative
        behaviour)

    Returns:
        dict if frontmatter parsed successfully, else None.
    """
    if not md_path.is_file():
        return None
    try:
        with md_path.open("r", encoding="utf-8") as f:
            first = f.readline()
            if first.rstrip("\n") != "---":
                return None
            lines: list[str] = []
            for line in f:
                if line.rstrip("\n") == "---":
                    break
                lines.append(line)
                if len(lines) > 500:
                    return None
            else:
                # Ran off end of file without a closing `---`.
                return None
    except OSError:
        return None
    try:
        import yaml
        meta = yaml.safe_load("".join(lines))
    except Exception as e:
        log.warning("could not parse frontmatter of %s: %s", md_path, e)
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def compose_md(
    frontmatter_fields: dict[str, Any],
    body: str,
) -> str:
    """Serialize frontmatter + body into final md text.

    Ensures consistent formatting: LF line endings, trailing newline,
    YAML with default_flow_style=False for readability.
    """
    post = frontmatter.Post(body, **frontmatter_fields)
    # frontmatter.dumps uses yaml.safe_dump internally; make sure we
    # don't get surprising flow style for lists.
    text = frontmatter.dumps(post, default_flow_style=False, allow_unicode=True)
    if not text.endswith("\n"):
        text += "\n"
    return text


def inject_preserved(body: str, preserved: PreservedContent) -> str:
    """Given a freshly generated body, inject preserved region contents.

    The body is expected to contain the standard region markers (start
    and end pairs). We locate the markers and replace whatever is
    between them with the preserved content.

    If preserved.ai_zone_body is None, we inject the default placeholder.
    Same for fulltext.
    """
    body = _replace_between(
        body,
        AI_ZONE_START,
        AI_ZONE_END,
        preserved.ai_zone_body or f"\n{AI_ZONE_PLACEHOLDER}\n",
    )
    body = _replace_between(
        body,
        FULLTEXT_START,
        FULLTEXT_END,
        preserved.fulltext_body or f"\n{FULLTEXT_PLACEHOLDER}\n",
    )
    # 1.3.0: re-append the Revisits section if one was preserved.
    # md_builder doesn't emit this section at all (revisits are
    # added by kb-write re-summarize --mode append, not by the
    # importer). So a template rebuild would drop it without this
    # splice. We append verbatim, after all template-generated body,
    # mirroring where re_summarize originally put it.
    if preserved.revisits_section:
        # Ensure one blank line separator so the new section doesn't
        # glue onto the end of the ai-zone without a visual break.
        body = body.rstrip() + "\n\n" + preserved.revisits_section.rstrip() + "\n"
    return body


def _replace_between(
    text: str, start_marker: str, end_marker: str, new_inner: str
) -> str:
    """Replace whatever's between the markers (exclusive) with new_inner.

    If the markers aren't both present, returns text unchanged — callers
    shouldn't rely on this silently succeeding; they should ensure the
    template includes both markers.
    """
    pattern = re.escape(start_marker) + r"(.*?)" + re.escape(end_marker)
    replacement = start_marker + new_inner + end_marker
    return re.sub(pattern, lambda _m: replacement, text, count=1, flags=re.DOTALL)


def merge_kb_frontmatter(
    new_fields: dict[str, Any],
    preserved_kb: dict[str, Any],
) -> dict[str, Any]:
    """Merge preserved kb_* fields into the new frontmatter dict.

    Preserved values win over any kb_* values in new_fields (normally
    new_fields shouldn't contain kb_*, but defensively: the contract
    is that kb_importer doesn't own kb_* and won't overwrite them).
    """
    merged = dict(new_fields)
    for k, v in preserved_kb.items():
        merged[k] = v
    return merged


def inject_fulltext(
    md_path: Path,
    fulltext_body: str,
    source_meta: dict[str, Any],
) -> None:
    """Replace the fulltext region of an existing paper md.

    Unlike `build_paper_md` + rewrite (which re-generates everything
    from Zotero), this surgical update keeps ALL other content
    unchanged — including the paper title, abstract, attachments
    section, Zotero notes section, AI zone, and any kb_* fields. Only
    the fulltext region body and a small set of fulltext_* frontmatter
    fields are touched.

    Args:
        md_path: path to the paper md (must exist).
        fulltext_body: the text to put between the fulltext markers.
            Should NOT include the markers themselves. Leading/trailing
            blank lines will be normalized.
        source_meta: dict of frontmatter fields to merge in (e.g.
            {"fulltext_processed": True, "fulltext_source": "zotero_note",
             "fulltext_processed_at": "2026-04-22T10:00:00Z"}).
            Keys starting with kb_ are rejected (not kb-importer's domain).

    Atomic: write goes through atomic_write so a crash mid-write
    cannot corrupt the md.
    """
    if not md_path.exists():
        raise FileNotFoundError(f"md not found: {md_path}")

    for k in source_meta:
        if k.startswith("kb_"):
            raise ValueError(
                f"inject_fulltext refuses to touch kb_* field {k!r}; "
                "those belong to the AI layer."
            )

    post = read_md(md_path)

    # Normalize body: single blank line padding before/after, so the
    # resulting md renders cleanly regardless of what caller passed in.
    body = fulltext_body.strip("\n")
    wrapped = f"\n{body}\n"

    has_start = FULLTEXT_START in post.content
    has_end = FULLTEXT_END in post.content

    if has_start and has_end:
        # Normal path: surgical splice between existing markers.
        new_content = _replace_between(
            post.content, FULLTEXT_START, FULLTEXT_END, wrapped,
        )
    else:
        # Self-heal path for pre-v21 mds (or any md that lost its
        # fulltext markers). Previously we raised ValueError here,
        # which meant `--force-fulltext` on legacy data would abort
        # before doing anything useful — forcing the user to first
        # run a metadata re-import to regenerate markers, then retry.
        # Instead, synthesize the markers in place.
        #
        # Placement rules:
        #   1. If an AI zone exists, put the new fulltext region
        #      IMMEDIATELY BEFORE it (canonical layout from
        #      build_paper_md is: ...paper body... → fulltext region
        #      → `---` → AI zone).
        #   2. If no AI zone either, append to the end.
        #
        # No existing content is deleted. Any pre-existing summary-
        # looking text (e.g. an old-format "AI Summary 2024-05-20"
        # block written by a legacy tool) stays where it was — we
        # don't try to parse and clean it; caller's new summary goes
        # into a fresh, correctly-wrapped region alongside. The
        # legacy text becomes contextual body that the next metadata
        # re-import's extract_preserved will still see.
        log.warning(
            "%s: fulltext markers missing; synthesizing them "
            "(legacy md self-heal). Existing body content is "
            "preserved untouched; new summary goes into a fresh "
            "wrapped region.",
            md_path.name,
        )
        new_region = f"\n{FULLTEXT_START}{wrapped}{FULLTEXT_END}\n"
        if AI_ZONE_START in post.content:
            # Insert before the AI zone start marker.
            idx = post.content.index(AI_ZONE_START)
            # Walk back past the optional `---` separator line that
            # build_paper_md emits between fulltext region and AI zone.
            # Best-effort: find nearest preceding non-whitespace line
            # starting with `---`; if found, insert before it.
            head = post.content[:idx]
            # Keep head as-is; insertion happens at boundary before
            # any trailing `---` separator.
            sep_match = re.search(r"\n---\s*\n\s*$", head)
            if sep_match:
                head = head[:sep_match.start()]
                new_content = (
                    head + new_region + "\n---\n\n"
                    + post.content[idx:]
                )
            else:
                new_content = head + new_region + "\n" + post.content[idx:]
        else:
            # No AI zone either — just append.
            trail = "" if post.content.endswith("\n") else "\n"
            new_content = post.content + trail + new_region

    # Merge frontmatter.
    for k, v in source_meta.items():
        post.metadata[k] = v

    # Rewrite via frontmatter.dumps then atomic write.
    final_text = frontmatter.dumps(frontmatter.Post(new_content, **post.metadata))
    # frontmatter.dumps doesn't always end with a trailing newline; add one.
    if not final_text.endswith("\n"):
        final_text += "\n"
    atomic_write(md_path, final_text)
