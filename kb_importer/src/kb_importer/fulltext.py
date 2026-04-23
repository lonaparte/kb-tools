"""Fulltext extraction for kb-importer --fulltext.

Two-tier strategy (per docs/fulltext-design.md):

  Tier 1: Zotero indexed fulltext via API (pyzotero fulltext_item).
          Pre-cleaned, header/footer-stripped text. Fast.
  Tier 2: Local PDF extraction via pdfplumber or pypdf.
          Fallback when Zotero hasn't indexed yet or the attachment
          isn't indexable.

On miss at both tiers: return ("", "unavailable"). Caller decides
whether to skip the paper or log an error.

This module is deliberately transport-agnostic: it takes a
ZoteroReader (for Tier 1) and a storage_dir Path (for Tier 2), and
returns (text, source_label). No kb_write / md writeback here — that
lives in the import_cmd integration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger(__name__)


SOURCE_ZOTERO_API = "zotero_api"
SOURCE_PDFPLUMBER = "pdfplumber"
SOURCE_PYPDF = "pypdf"
SOURCE_UNAVAILABLE = "unavailable"

# Used by the long-form pipeline as the `source` field in writeback
# when generating the parent paper's chapter-index landing page.
# The actual chapter-split mechanism (bookmarks / regex / llm /
# fallback) goes in a separate `longform_split_source` frontmatter
# field so `fulltext_source` stays a single flat category alongside
# the other SOURCE_* values.
SOURCE_LONGFORM = "longform"

# Reasonable upper bound for a single paper's extracted text. Papers
# over this are almost certainly pathological (entire books; scanned
# junk). Truncation is safer than sending 2MB to an LLM and blowing
# the context window. The LLM summary just wants signal, not bulk.
MAX_FULLTEXT_CHARS = 200_000


@dataclass
class FulltextResult:
    """Outcome of one paper's fulltext extraction attempt."""

    paper_key: str
    text: str                # Empty string on miss.
    source: str              # One of SOURCE_* constants.
    pdf_path: str | None     # Path that was tried (if any).
    error: str | None        # Short human message on miss; None on success.

    @property
    def ok(self) -> bool:
        return bool(self.text)


def extract_fulltext(
    paper_key: str,
    attachments: list,          # list[ZoteroAttachment]
    reader,                     # ZoteroReader (for Tier 1)
    storage_dir: Path | None,   # for Tier 2; None → skip local
    truncate: bool = True,      # v22: long pipeline disables this
) -> FulltextResult:
    """Try Zotero API, then local PDF. Return the first success.

    Args:
        paper_key: parent paper's Zotero key (for logging / result label).
        attachments: the paper's ZoteroAttachment list. Ordering matters:
            we try each attachment in order. Usually the first is the
            main PDF.
        reader: ZoteroReader exposing `.fetch_fulltext(attachment_key)`.
        storage_dir: Zotero storage root. If None, Tier 2 is skipped
            entirely (useful in web mode without local storage sync).
        truncate: if True (default), cap output at MAX_FULLTEXT_CHARS
            (~200K) head+tail style — safe for short summary pipeline.
            Long pipeline MUST pass False so split_into_chapters sees
            the entire book (otherwise chapters in the middle 30% of
            the book vanish). v21 and earlier always truncated, which
            silently lost most of every book's content when it later
            got routed to the chapter splitter.

    Returns:
        FulltextResult with text populated on success. On total miss,
        `.ok` is False and `.error` carries a short explanation.
    """
    if not attachments:
        return FulltextResult(
            paper_key=paper_key,
            text="",
            source=SOURCE_UNAVAILABLE,
            pdf_path=None,
            error="no PDF attachment on this paper",
        )

    # Select head-tail truncate or identity based on caller choice.
    cap = _truncate if truncate else (lambda t: t)

    # ---- Tier 1: Zotero API ----
    # Try each attachment; first with non-empty indexed text wins.
    for att in attachments:
        try:
            text = reader.fetch_fulltext(att.key)
        except Exception as e:
            # reader should not raise, but belt-and-braces.
            log.debug(
                "reader.fetch_fulltext(%s) raised: %s", att.key, e
            )
            text = None
        if text:
            return FulltextResult(
                paper_key=paper_key,
                text=cap(text),
                source=SOURCE_ZOTERO_API,
                pdf_path=None,
                error=None,
            )

    # ---- Tier 2: local PDF ----
    if storage_dir is None:
        return FulltextResult(
            paper_key=paper_key,
            text="",
            source=SOURCE_UNAVAILABLE,
            pdf_path=None,
            error="Zotero had no fulltext and storage_dir not configured",
        )

    for att in attachments:
        pdf = _find_pdf(storage_dir, att.key, att.filename)
        if pdf is None:
            continue
        text, tool = _extract_from_pdf(pdf)
        if text:
            return FulltextResult(
                paper_key=paper_key,
                text=cap(text),
                source=tool,
                pdf_path=str(pdf),
                error=None,
            )

    return FulltextResult(
        paper_key=paper_key,
        text="",
        source=SOURCE_UNAVAILABLE,
        pdf_path=None,
        error="Zotero API had no fulltext and no extractable PDF "
              "found in storage",
    )


def _find_pdf(storage_dir: Path, att_key: str, filename: str) -> Path | None:
    """Locate a PDF on disk given (storage_dir, attachment_key, filename).

    Zotero layout: `<storage_dir>/<ATTACHMENT_KEY>/<filename>`.

    Fallback: if the exact filename doesn't match (e.g. Zotero renamed
    it), glob for `*.pdf` inside the attachment dir and return the
    first hit. More lenient than strict; helpful when users have
    changed Zotero's filename template.
    """
    att_dir = storage_dir / att_key
    if not att_dir.is_dir():
        return None

    exact = att_dir / filename
    if exact.is_file() and exact.suffix.lower() == ".pdf":
        return exact

    for p in sorted(att_dir.glob("*.pdf")):
        return p
    return None


def _extract_from_pdf(pdf: Path) -> tuple[str, str]:
    """Extract text from a local PDF. Try pdfplumber first; fall
    back to pypdf. Return (text, source_label).

    Returns ("", "unavailable") if both tools fail or neither is
    installed. Never raises — extraction failure should degrade to
    "this paper stays header-only".
    """
    # pdfplumber: best layout handling for academic PDFs (2-column,
    # tables, etc.) but slower and heavier dep.
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        pdfplumber = None  # type: ignore
    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(pdf)) as pdf_obj:
                pages = []
                for page in pdf_obj.pages:
                    t = page.extract_text() or ""
                    if t.strip():
                        pages.append(t)
                text = "\n\n".join(pages).strip()
            if text:
                return text, SOURCE_PDFPLUMBER
        except Exception as e:
            log.debug("pdfplumber failed on %s: %s", pdf, e)

    # pypdf: lighter, less accurate, but gets the job done on
    # single-column papers.
    try:
        import pypdf  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore  # legacy fallback
            pypdf = None
        except ImportError:
            PdfReader = None  # type: ignore
            pypdf = None  # type: ignore
        else:
            pass
    else:
        PdfReader = pypdf.PdfReader

    if PdfReader is not None:
        try:
            reader = PdfReader(str(pdf))
            pages = []
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(t)
            text = "\n\n".join(pages).strip()
            if text:
                return text, SOURCE_PYPDF
        except Exception as e:
            log.debug("pypdf failed on %s: %s", pdf, e)

    return "", SOURCE_UNAVAILABLE


def _truncate(text: str) -> str:
    """Cap text to MAX_FULLTEXT_CHARS, preserving head + tail.

    Most papers have the introduction and conclusion as the most
    signal-dense sections; losing the middle is less bad than losing
    both. Split budget roughly 70/30 head/tail.
    """
    if len(text) <= MAX_FULLTEXT_CHARS:
        return text
    head = int(MAX_FULLTEXT_CHARS * 0.7)
    tail = MAX_FULLTEXT_CHARS - head - 100  # -100 for ellipsis marker
    return (
        text[:head]
        + "\n\n[... fulltext truncated ...]\n\n"
        + text[-tail:]
    )
