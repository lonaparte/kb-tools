"""Build markdown files from ZoteroItem objects.

Owns the templates defined in spec §4.3 and §4.4. Produces text that,
combined with inject_preserved(), becomes the final md content.

This module knows both Zotero and our md format, which is unavoidable —
it's the bridge. Everything else is split cleanly between sides.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from markdownify import markdownify as html_to_md

from .md_io import (
    AI_ZONE_END,
    AI_ZONE_PLACEHOLDER,
    AI_ZONE_START,
    FULLTEXT_END,
    FULLTEXT_PLACEHOLDER,
    FULLTEXT_START,
    PreservedContent,
    compose_md,
    inject_preserved,
    merge_kb_frontmatter,
)
from .zotero_reader import ZoteroAttachment, ZoteroItem, ZoteroNote


# Default values for kb_* fields when a md is created fresh.
DEFAULT_KB_FIELDS = {
    "kb_tags": [],
    "kb_refs": [],
    "kb_topics": [],
    "kb_last_touched": None,
}


def build_paper_md(
    item: ZoteroItem,
    preserved: PreservedContent,
    attachment_locations: list[tuple[ZoteroAttachment, str | None, bool]] | None = None,
) -> str:
    """Build the full md text for a paper.

    Args:
        item: The Zotero paper item (with child notes + attachments).
        preserved: Content to keep from any existing md at this path.
        attachment_locations: per-attachment (attachment, rel_path, is_archived)
            tuples in the same order as item.attachments. rel_path is None
            if the PDF isn't on disk. If not provided, defaults to empty.
    """
    if attachment_locations is None:
        attachment_locations = []
    fm = _build_paper_frontmatter(item)
    fm = merge_kb_frontmatter(fm, preserved.kb_frontmatter)
    # fulltext_* fields preserved across re-renders (see md_io for
    # rationale).
    for k, v in preserved.fulltext_frontmatter.items():
        fm[k] = v

    # Apply user overrides that still reference valid data. Currently
    # only zotero_main_attachment_key: if the user hand-picked a
    # different main PDF, honour that — but only if the key still
    # matches one of the current attachments (otherwise fall back to
    # the heuristic pick from _build_paper_frontmatter).
    current_att_keys = {a.key for a in item.attachments}
    override_main = preserved.user_override_fields.get("zotero_main_attachment_key")
    if override_main and override_main in current_att_keys:
        fm["zotero_main_attachment_key"] = override_main

    # If the preserved frontmatter records that some child notes have
    # already been migrated into the fulltext region (via import-summaries),
    # we exclude them from the Zotero Notes rendering so the same text
    # doesn't appear twice in the md. The source of truth is the
    # `fulltext_source_note_keys` field, which import-summaries writes.
    migrated_keys = _extract_migrated_note_keys(preserved.fulltext_frontmatter)

    body = _build_paper_body(
        item, attachment_locations, migrated_note_keys=migrated_keys,
    )
    body = inject_preserved(body, preserved)
    return compose_md(fm, body)


def _extract_migrated_note_keys(fulltext_frontmatter: dict) -> set[str]:
    """Pull `fulltext_source_note_keys` out of preserved fulltext_*
    frontmatter (if present and list-shaped). Returns empty set if absent.

    We only care about this when fulltext_source is "zotero_note" — for
    "external" summaries there's nothing to de-duplicate against.
    """
    if fulltext_frontmatter.get("fulltext_source") != "zotero_note":
        return set()
    keys = fulltext_frontmatter.get("fulltext_source_note_keys") or []
    if not isinstance(keys, list):
        return set()
    return {k for k in keys if isinstance(k, str)}


def build_note_md(
    item: ZoteroItem,
    preserved: PreservedContent,
) -> str:
    """Build the full md text for a standalone note."""
    fm = _build_note_frontmatter(item)
    fm = merge_kb_frontmatter(fm, preserved.kb_frontmatter)
    body = _build_note_body(item)
    body = inject_preserved(body, preserved)
    return compose_md(fm, body)


# ----------------------------------------------------------------------
# Frontmatter builders
# ----------------------------------------------------------------------

# Filename substrings suggesting a non-main PDF. Case-insensitive match.
# Order doesn't matter; any match disqualifies an attachment from being
# picked as "main" unless it's the ONLY attachment.
_NON_MAIN_FILENAME_HINTS = (
    "supp", "supplement", "supplementary",
    "appendix",
    "si.pdf", "-si.", "_si.",
    "cover-letter", "coverletter",
    "response", "rebuttal", "reviewer",
    "errata", "erratum",
    "slides", "presentation", "poster",
    "arxiv-cover",
)


def _pick_main_attachment_key(attachments: list[ZoteroAttachment]) -> str | None:
    """Heuristic pick of the "main" PDF from a paper's attachments.

    The rules, in decreasing priority:
      1. If there are zero attachments → None.
      2. If there's exactly one → that one (even if the filename
         looks like a supplement — at least it's all we have).
      3. Among attachments whose filename does NOT match any of
         _NON_MAIN_FILENAME_HINTS, pick the one with the earliest
         dateAdded (typically the first-added PDF is the canonical
         text). Stable tiebreak by Zotero-returned order.
      4. If ALL attachments look like non-main (weird but possible
         if Zotero only holds supplementary material), fall back to
         rule 3 over the full list.

    This is deliberately conservative: we don't inspect PDF bytes
    (that would require opening files). The list of hints is not
    exhaustive; users whose filenames defeat it can manually edit
    `zotero_main_attachment_key` in the md, and kb-importer will
    preserve their override on subsequent syncs.
    """
    if not attachments:
        return None
    if len(attachments) == 1:
        return attachments[0].key

    def looks_non_main(att: ZoteroAttachment) -> bool:
        name = (att.filename or "").lower()
        return any(hint in name for hint in _NON_MAIN_FILENAME_HINTS)

    candidates = [a for a in attachments if not looks_non_main(a)]
    pool = candidates or attachments  # rule 4 fallback
    # Earliest dateAdded wins; stable on insertion order for ties.
    # Empty dateAdded sorts as "" which is before any real ISO ts,
    # which would be wrong — treat empty as "last" so it doesn't
    # outrank a real PDF.
    sentinel_last = "\uffff"
    pool_sorted = sorted(
        enumerate(pool),
        key=lambda pair: (pair[1].date_added or sentinel_last, pair[0]),
    )
    return pool_sorted[0][1].key


def _build_paper_frontmatter(item: ZoteroItem) -> dict:
    # Child-note version tracking: Zotero assigns each item (including
    # each child note) its own version number, but editing a note does
    # NOT bump the parent paper's version. So sync must check these
    # separately. We store both the max version across all child notes
    # and the count — the count catches the case where a note was
    # deleted (existing versions stay the same, so max wouldn't change).
    child_versions = [n.version for n in item.notes] if item.notes else []
    max_child_version = max(child_versions) if child_versions else 0

    # Attachment tracking: identical shape to child-note tracking, and
    # for identical reasons. Each attachment is a separate Zotero item
    # with its own version; adding/removing/editing an attachment
    # doesn't bump the paper's version. The attachment_keys list is
    # also the reverse-lookup hook the Indexer will use to map
    # attachment key → paper key (see spec §6).
    att_versions = [a.version for a in item.attachments] if item.attachments else []
    max_att_version = max(att_versions) if att_versions else 0

    # Identify the "main" PDF among possibly-several attachments.
    # Zotero doesn't flag one, so we heuristic: prefer filenames that
    # don't look like supplementary / appendix material; tie-break by
    # earliest dateAdded (main PDF is usually added first). Stored as
    # a separate frontmatter field so agents can use it directly
    # without having to glob or guess.
    main_att_key = _pick_main_attachment_key(item.attachments)

    fm: dict = {
        "zotero_key": item.key,
        "zotero_version": item.version,
        "zotero_max_child_version": max_child_version,
        "zotero_child_note_count": len(item.notes),
        # Attachment keys in Zotero-returned order. Storage subdirs are
        # named by these keys, NOT the paper key.
        "zotero_attachment_keys": [a.key for a in item.attachments],
        # Heuristic "main" PDF (see _pick_main_attachment_key). None if
        # the paper has no PDF attachments. Agents wanting "the PDF"
        # should use this; consumers wanting ALL PDFs should iterate
        # zotero_attachment_keys.
        "zotero_main_attachment_key": main_att_key,
        "zotero_max_attachment_version": max_att_version,
        "zotero_attachment_count": len(item.attachments),
        "kind": "paper",
        "item_type": item.item_type,
        "title": item.title,
        "authors": list(item.authors),
        "year": item.year,
        "date": item.date,
        "publication": item.publication,
        "doi": item.doi,
        "url": item.url,
        "citation_key": item.citation_key,
        "zotero_tags": list(item.tags),
        "zotero_collections": list(item.collections),
        "date_added": item.date_added,
        "date_modified": item.date_modified,
        "imported_at": _now_iso(),
        "fulltext_processed": False,
        "fulltext_processed_at": None,
        "fulltext_model": None,
    }
    # Add kb_* defaults; these get overwritten by preserved values in
    # merge_kb_frontmatter, but they're needed for the schema to be
    # stable when a md is created for the first time.
    fm.update(DEFAULT_KB_FIELDS)
    return fm


def _build_note_frontmatter(item: ZoteroItem) -> dict:
    fm: dict = {
        "zotero_key": item.key,
        "zotero_version": item.version,
        # v27: prefer `kind: note` (short, matches the NodeAddress
        # node_type "note"). Indexer accepts both "note" and the
        # legacy "zotero_standalone_note" for backward compatibility
        # with mds written by earlier versions — no auto-migration
        # is performed on existing files. See
        # kb_mcp.indexer._ingest_note for the accept-both logic.
        "kind": "note",
        "title": item.title or _first_line_as_title(item.notes),
        "zotero_tags": list(item.tags),
        "date_added": item.date_added,
        "date_modified": item.date_modified,
        "imported_at": _now_iso(),
    }
    fm.update(DEFAULT_KB_FIELDS)
    return fm


# ----------------------------------------------------------------------
# Body builders
# ----------------------------------------------------------------------

def _build_paper_body(
    item: ZoteroItem,
    attachment_locations: list[tuple[ZoteroAttachment, str | None, bool]],
    migrated_note_keys: set[str] | None = None,
) -> str:
    """Render a paper's md body.

    attachment_locations: one entry per attachment in item.attachments,
    in the same order. Each entry is (attachment, rel_path, is_archived):
    - rel_path: path to the PDF on disk, relative to the paper md file,
      or None if the PDF wasn't found in storage.
    - is_archived: True if the PDF was found under _archived/ (only
      meaningful when rel_path is not None).

    migrated_note_keys: keys of child notes that have been migrated into
    the fulltext region by `import-summaries`. These are EXCLUDED from
    the Zotero Notes section to avoid duplicate content appearing in
    two places. If None or empty, all notes are rendered.
    """
    if migrated_note_keys is None:
        migrated_note_keys = set()
    lines: list[str] = []

    lines.append(f"# {item.title or 'Untitled'}")
    lines.append("")

    if item.authors:
        lines.append(f"**Authors**: {_format_authors(item.authors)}")
    if item.year:
        lines.append(f"**Year**: {item.year}")
    if item.item_type:
        lines.append(f"**Type**: {item.item_type}")
    if item.publication:
        lines.append(f"**Publication**: {item.publication}")
    if item.doi:
        lines.append(f"**DOI**: [{item.doi}](https://doi.org/{item.doi})")
    if item.url:
        lines.append(f"**URL**: <{item.url}>")
    lines.append("")

    if item.abstract:
        lines.append("## Abstract")
        lines.append("")
        lines.append("<!-- zotero-field: abstractNote -->")
        lines.append(item.abstract.strip())
        lines.append("")

    # Zotero notes section (child notes), sorted by dateAdded DESC.
    # Notes that have been migrated into the fulltext region by
    # `import-summaries` are excluded here to prevent the same content
    # from appearing twice (once under "Zotero Notes" and once under
    # "AI Summary (from Full Text)"). The list of migrated keys comes
    # from the `fulltext_source_note_keys` frontmatter field and is
    # passed down via `migrated_note_keys`.
    visible_notes = [
        n for n in item.notes if n.key not in migrated_note_keys
    ]
    if visible_notes:
        lines.append("## Zotero Notes")
        lines.append("")
        # NOTE: `<!-- zotero-notes-start -->` is a historical zone
        # marker name (not a directory path). It marks the Zotero
        # child-notes region inside a paper md body. The v26 layout
        # rename (zotero-notes/ → topics/standalone-note/) is about
        # the on-disk location for STANDALONE notes, a different
        # concept — these markers are for CHILD notes embedded in a
        # paper md, and renaming them would break the zone parser
        # for every existing paper md in the wild. Keep as-is.
        lines.append("<!-- zotero-notes-start -->")
        sorted_notes = sorted(
            visible_notes,
            key=lambda n: n.date_added or "",
            reverse=True,
        )
        for note in sorted_notes:
            lines.extend(_render_child_note(note))
        lines.append("<!-- zotero-notes-end -->")
        lines.append("")

    # Attachments section.
    lines.append("## Attachments")
    lines.append("")
    lines.append(f"- [Open in Zotero](zotero://select/items/{item.key})")
    if not attachment_locations:
        lines.append("- (no PDF attachments in Zotero)")
    else:
        # One bullet per attachment. We show the filename Zotero assigned
        # as the label (more meaningful than the key), plus the relative
        # path, plus its attachment key (so AI / reader can correlate
        # with storage/ subdirs).
        for att, rel_path, archived in attachment_locations:
            label = att.filename or "(unnamed)"
            if rel_path is None:
                lines.append(
                    f"- `{label}` — PDF not found locally "
                    f"(attachment key `{att.key}`)"
                )
            else:
                tag = " (archived)" if archived else ""
                lines.append(
                    f"- [{label}]({rel_path}){tag} "
                    f"— attachment key `{att.key}`"
                )
    lines.append("")

    # Fulltext region (empty in metadata-only mode; inject_preserved
    # replaces it with preserved content if any).
    lines.append("## AI Summary (from Full Text)")
    lines.append("")
    lines.append(FULLTEXT_START)
    lines.append(FULLTEXT_PLACEHOLDER)
    lines.append(FULLTEXT_END)
    lines.append("")

    # AI zone (same pattern).
    lines.append("---")
    lines.append("")
    lines.append(AI_ZONE_START)
    lines.append(AI_ZONE_PLACEHOLDER)
    lines.append(AI_ZONE_END)

    return "\n".join(lines)


def _build_note_body(item: ZoteroItem) -> str:
    lines: list[str] = []

    title = item.title or _first_line_as_title(item.notes) or "Untitled Note"
    lines.append(f"# {title}")
    lines.append("")

    # The note's own content is the single element in item.notes.
    lines.append("<!-- zotero-note-content-start -->")
    if item.notes:
        note_md = _html_to_md(item.notes[0].html)
        lines.append(note_md)
    lines.append("<!-- zotero-note-content-end -->")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(AI_ZONE_START)
    lines.append(AI_ZONE_PLACEHOLDER)
    lines.append(AI_ZONE_END)

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _render_child_note(note: ZoteroNote) -> list[str]:
    lines: list[str] = []
    date_short = (note.date_added or "")[:10]  # YYYY-MM-DD
    lines.append(f"<!-- zotero-note-key: {note.key} -->")
    title = f"### Note ({date_short})" if date_short else "### Note"
    lines.append(title)
    lines.append("")
    lines.append(_html_to_md(note.html))
    lines.append("")
    return lines


def _html_to_md(html: str) -> str:
    """Convert Zotero note HTML to markdown.

    Empty or whitespace-only HTML returns empty string (not filtered —
    spec §4.6 says don't filter empty notes).
    """
    if not html:
        return ""
    try:
        md = html_to_md(html, heading_style="ATX")
    except Exception:
        # markdownify failed — return the raw HTML as fallback so no
        # information is silently lost.
        return html
    return md.strip()


def _format_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) > 5:
        head = ", ".join(authors[:3])
        return f"{head}, et al."
    return ", ".join(authors)


def _first_line_as_title(notes: list[ZoteroNote]) -> str:
    """Fallback title for standalone notes: first plain-text line."""
    if not notes:
        return ""
    text = _html_to_md(notes[0].html).strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    # Strip leading markdown header marks
    while first_line.startswith("#"):
        first_line = first_line[1:].lstrip()
    return first_line[:120]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def paper_md_path(kb_root: Path, zotero_key: str) -> Path:
    return kb_root / "papers" / f"{zotero_key}.md"


def note_md_path(kb_root: Path, zotero_key: str) -> Path:
    # v26: standalone Zotero notes live under topics/standalone-note/.
    return kb_root / "topics" / "standalone-note" / f"{zotero_key}.md"
