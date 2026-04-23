"""Writeback: splice the 7-section summary into a paper's md body,
and update frontmatter to record (fulltext_processed, source, date).

Design:
  The writeback is a SURGICAL splice, not a wholesale body replacement.
  It only modifies the region between `<!-- kb-fulltext-start -->` and
  `<!-- kb-fulltext-end -->`, leaving the AI zone, attachments section,
  Zotero notes section, and any user-added content intact.

  A previous version did `post.content = summary_markdown`, which
  produced a clean-looking summary but deleted all other markers and
  sections. That broke subsequent metadata re-imports (they couldn't
  find the markers, so extract_preserved returned empty, so the summary
  was silently dropped — while fulltext_processed stayed true, so
  --fulltext would skip the paper). Result: permanent data loss on the
  next re-import. inject_fulltext fixes this by design.

Invariants we respect:
  - YAML frontmatter stays on top, triple-dash delimited.
  - The 7 H2 sections go INSIDE the fulltext markers. Re-running
    with --force-fulltext overwrites the section content but keeps
    the rest of the md untouched.
  - We never touch Zotero-owned frontmatter (title/authors/year/doi/
    item_type/abstract/zotero_key) or kb_* fields (kb_tags, kb_refs).
    Only add/update fulltext_* keys:
      fulltext_processed: true
      fulltext_source: zotero_api | pdfplumber | pypdf
      fulltext_extracted_at: ISO timestamp
      fulltext_model: <provider>/<model>
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from .md_io import inject_fulltext


log = logging.getLogger(__name__)


FULLTEXT_FRONTMATTER_KEYS = (
    "fulltext_processed",
    "fulltext_source",
    "fulltext_extracted_at",
    "fulltext_model",
)


def is_fulltext_processed(md_path: Path) -> bool:
    """Quick check — True iff md's frontmatter has fulltext_processed=true.

    Uses md_io.peek_frontmatter which streams only the YAML header,
    not the body (matters for 1000+ paper scans). Missing file /
    missing frontmatter / unparseable YAML all return False so the
    caller will conservatively attempt processing again.
    """
    from .md_io import peek_frontmatter
    meta = peek_frontmatter(md_path)
    if meta is None:
        return False
    return bool(meta.get("fulltext_processed"))


def writeback_summary(
    md_path: Path,
    *,
    summary_markdown: str,
    source: str,
    model_label: str,
) -> None:
    """Splice the 7-section summary into the fulltext region of the md,
    preserving ALL other content (AI zone, attachments section, Zotero
    notes section, frontmatter fields we don't own, etc.).

    Uses `inject_fulltext()` from md_io — the same helper Zotero-sync
    re-import uses — so re-running `kb-importer import --fulltext` on
    a paper produces a byte-identical result regardless of what other
    content (ai_zone edits, custom notes) the user has added.

    IMPORTANT: This replaces ONLY the content between
    `<!-- kb-fulltext-start -->` and `<!-- kb-fulltext-end -->`.
    Previous versions used `post.content = summary_markdown`, which
    replaced the entire body and silently erased AI zone markers,
    attachment sections, and custom Zotero notes. The missing markers
    then broke the next metadata re-import (extract_preserved couldn't
    find them), causing the summary to be lost permanently while
    fulltext_processed remained true → --fulltext would skip it.

    Args:
        md_path: existing paper md. If it doesn't exist, FileNotFoundError.
        summary_markdown: the 7-section H2 body (from
            SummaryResult.to_markdown). Goes between the markers; no
            need for the caller to add markers.
        source: one of the fulltext.SOURCE_* values.
        model_label: "gemini/gemini-3.1-pro-preview", etc.

    Raises:
        FileNotFoundError: md_path doesn't exist.
        ValueError: md is malformed — fulltext markers are missing.
            This indicates either a corrupted md or one not generated
            by kb-importer; the caller should log and skip.
    """
    if not md_path.is_file():
        raise FileNotFoundError(md_path)

    extracted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Delegate to the canonical splice helper; it raises ValueError
    # if markers are missing. Source_meta covers only fulltext_* keys
    # (inject_fulltext rejects kb_* writes).
    inject_fulltext(
        md_path,
        fulltext_body=summary_markdown,
        source_meta={
            "fulltext_processed": True,
            "fulltext_source": source,
            "fulltext_extracted_at": extracted_at,
            "fulltext_model": model_label,
        },
    )
