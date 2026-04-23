"""Stage 1 of the long-form pipeline: split a book/thesis full text
into chapters.

Three detection strategies, tried in order:

  1. PDF bookmarks (outline). Cheapest and most accurate when the PDF
     has a real ToC. pdfplumber exposes `pdf.outlines`; we walk it to
     recover chapter titles + start pages, then slice page text by
     page range.

  2. Regex on the plain text. Matches patterns like `^Chapter N+`,
     `^第N章`, `^N. Title`. Works on well-formatted scholarly books
     and most theses. Falls through when layout is unusual (journals,
     edited volumes with per-section authors, scanned PDFs where text
     extraction lost whitespace).

  3. LLM-assisted. Last resort: send the first ~50 pages to a cheap
     model (Gemini Flash), ask for a JSON list of chapter titles and
     start positions. Costs ~$0.005/book. Gated on a flag passed by
     the caller so we can measure which books end up needing this.

If all three fail we return a single-chapter result containing the
whole text — the long pipeline then degrades gracefully into
"one big chapter" (not ideal, but still produces one thought instead
of silently losing the paper).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger(__name__)


# Hard cap on chapters we'll produce per book. More than this almost
# always means the splitter went haywire (e.g. matched every numbered
# figure caption as a chapter). At that point better to report failure
# and let the user inspect manually than to blast 200 LLM calls.
MAX_CHAPTERS = 60

# Minimum chapter text length to be considered real. Real book
# chapters run 5K-30K chars; sub-1500 "chapters" are almost always
# false positives (index entries, TOC line items, per-page headers,
# figure captions that happen to start with "Chapter N presents...").
#
# History:
#   v22: 300 chars — too permissive; thesis TOCs squeaked through.
#   v23: 500 chars — caught most TOC windows but figure captions and
#        list-of-figures blocks still passed.
#   v24: 1500 chars — based on empirical v22 run data (210 chapter
#        thoughts, 28 garbage). 1500 rejects figure/table blocks and
#        bibliography entries while leaving real chapters untouched.
MIN_CHAPTER_CHARS = 1_500

# If regex-splitter's output averages below this per chapter, the
# split is almost certainly a false-positive storm even if individual
# chapters clear MIN_CHAPTER_CHARS. Real book chapters average 10K-
# 30K chars; a per-chapter mean below 2K is dispositive evidence the
# pattern matched TOC entries or figure captions rather than chapter
# bodies. On such a result we REJECT the regex output entirely so the
# next strategy (LLM, then fallback) can run.
REGEX_SUSPICIOUSLY_SHORT_MEAN = 2_000

# Front-matter and back-matter section titles. When the regex matches
# one of these as a "chapter", drop it — these are never what we want
# as a chapter summary (v22 produced 18/210 garbage thoughts from
# these: list-of-figures, bibliography, series-preface, references,
# index, etc). Case-insensitive match against the chapter title line.
_BOOK_NON_CHAPTER_TITLE_RE = re.compile(
    r"^\s*(chapter\s+\d+\.?\s+)?"  # optional "Chapter N" prefix
    r"("
    r"preface|foreword|acknowledgements?|acknowledgments?|"
    r"references?|bibliography|works\s+cited|"
    r"index|glossary|nomenclature|notation|symbols?|abbreviations?|"
    r"table\s+of\s+contents?|contents?|"
    r"list\s+of\s+(figures?|tables?|papers?|abbreviations?|symbols?)|"
    r"appendix(\s+[a-z0-9]+)?|"
    r"series\s+preface|editors?['’]\s+preface|"
    r"about\s+the\s+(author|editor)s?|"
    r"dedication|colophon|copyright|imprint|"
    r"errata|addendum"
    r")"
    r"\b.*$",
    flags=re.IGNORECASE,
)

# Figure / table / equation caption prefixes. These are never
# chapters — they're items WITHIN a chapter. v22 produced 7/210
# garbage thoughts from these: fig-3-2-three-types-of-erro,
# figure-7-1-attenuation-char, table-5-1-parameters-used-i, etc.
_CAPTION_TITLE_RE = re.compile(
    r"^\s*(figure|fig\.?|table|tbl\.?|equation|eq\.?|scheme|plate)"
    r"\s*[0-9IVXLC]",  # followed by a number (arabic or roman)
    flags=re.IGNORECASE,
)

# Numeric-density threshold. A "chapter" whose body is >80% digits +
# punctuation is almost certainly a data table or bibliography
# column, not real prose. v22 produced entries like
# "ch21-600-a1-105-95-160-910-262-320" (a pure data row). Check is
# cheap (O(n) char scan) and only runs on sub-threshold candidates
# anyway, so no perf impact on well-formed books.
NUMERIC_DENSITY_THRESHOLD = 0.80

# Max chars per chapter before we force a subsection split. At ~200K
# chars and Gemini 3.1 Pro's thinking overhead, we start losing
# summary quality. 60K is a safe middle ground: ~15K tokens input,
# plenty of headroom for the 7-section response.
MAX_CHAPTER_CHARS = 60_000


def _looks_like_front_or_back_matter(title: str) -> bool:
    """True if the chapter title is a well-known non-chapter section
    (preface, references, index, list-of-figures, etc.).
    """
    return bool(_BOOK_NON_CHAPTER_TITLE_RE.match(title or ""))


def _looks_like_caption(title: str) -> bool:
    """True if the chapter title looks like a figure/table caption
    rather than a chapter heading (Figure 3.2, Table 5-1, etc.).
    """
    return bool(_CAPTION_TITLE_RE.match(title or ""))


def _is_data_blob(text: str) -> bool:
    """True if the candidate body is >80% digits+punctuation (i.e. a
    data table dump rather than prose). Empty/very short text returns
    False so the MIN_CHAPTER_CHARS check handles it instead.
    """
    if len(text) < 200:
        return False
    # Count "non-prose" chars: digits, punctuation, whitespace.
    # Any letter (ascii or CJK) counts as prose.
    prose = sum(1 for ch in text if ch.isalpha())
    ratio_non_prose = 1.0 - (prose / len(text))
    return ratio_non_prose > NUMERIC_DENSITY_THRESHOLD


@dataclass
class Chapter:
    """A single chapter extracted from a book/thesis."""

    number: int            # 1-indexed position in the output list
    title: str             # "Chapter 5: RMS Measurement" or similar
    text: str              # chapter body (may be subsection-split further)
    pages: str | None = None  # "142-167" if we know, else None
    # Did we subsection-split this? Internal diagnostic.
    subsection_of: int | None = None


@dataclass
class SplitResult:
    """Outcome of one book's chapter detection."""

    chapters: list[Chapter]
    source: str  # "bookmarks" | "regex" | "llm" | "single_chapter_fallback"
    # Retry counts etc. for diagnostics. Kept as dict so we can add
    # more fields without breaking API.
    diagnostics: dict = field(default_factory=dict)


class SplitError(Exception):
    """Raised only when all three strategies fail AND even the
    single-chapter fallback is unusable (e.g. empty text). Caller
    should skip the paper.
    """


def split_into_chapters(
    fulltext: str,
    pdf_path: str | None = None,
    *,
    allow_llm_fallback: bool = True,
    llm_provider=None,  # kb_importer.summarize.LLMProvider | None
) -> SplitResult:
    """Split `fulltext` into chapters. Tries bookmarks → regex → LLM.

    Args:
        fulltext: the already-extracted plain text of the book/thesis.
            Assumed to be in reading order (pdfplumber's extract_text
            gives this for most PDFs). May contain hard page breaks
            or form-feeds; we handle both.
        pdf_path: optional path to the original PDF. Only used by the
            bookmarks strategy; if None or the PDF isn't readable,
            we skip straight to regex.
        allow_llm_fallback: if False, don't try the LLM path even when
            regex fails. Used by the dryrun mode and by tests.
        llm_provider: kb_importer.summarize.LLMProvider to use for the
            LLM path. If None, LLM path is skipped.

    Returns:
        SplitResult with at least one chapter. Falls back to a single
        whole-text chapter if all strategies fail — the caller can
        still produce a summary, just without chapter-level index.

    Raises:
        SplitError: only when fulltext is empty / all-whitespace.
    """
    if not fulltext or not fulltext.strip():
        raise SplitError("cannot split empty fulltext")

    # --- Strategy 1: PDF bookmarks ---
    if pdf_path:
        try:
            chapters = _split_by_bookmarks(pdf_path, fulltext)
        except Exception as e:
            log.debug("bookmark split failed on %s: %s", pdf_path, e)
            chapters = None
        if chapters and _is_plausible(chapters):
            return SplitResult(
                chapters=_apply_chapter_caps(chapters),
                source="bookmarks",
                diagnostics={"pdf_path": pdf_path},
            )

    # --- Strategy 2: regex ---
    chapters = _split_by_regex(fulltext)
    if chapters and _is_plausible(chapters):
        return SplitResult(
            chapters=_apply_chapter_caps(chapters),
            source="regex",
            diagnostics={},
        )

    # --- Strategy 3: LLM ---
    if allow_llm_fallback and llm_provider is not None:
        try:
            chapters = _split_by_llm(fulltext, llm_provider)
        except Exception as e:
            log.warning("LLM split failed: %s", e)
            chapters = None
        if chapters and _is_plausible(chapters):
            return SplitResult(
                chapters=_apply_chapter_caps(chapters),
                source="llm",
                diagnostics={},
            )

    # --- Fallback: single chapter ---
    # Better than failing outright: at least produce one summary, even
    # if it's going to be a coarse "whole book" summary. Caller will
    # see split_source="single_chapter_fallback" in the report and
    # can investigate why detection failed.
    log.warning(
        "all split strategies failed; falling back to single-chapter "
        "mode (%d chars)", len(fulltext),
    )
    return SplitResult(
        chapters=[Chapter(number=1, title="(full text)", text=fulltext)],
        source="single_chapter_fallback",
        diagnostics={"len": len(fulltext)},
    )


# ----------------------------------------------------------------------
# Strategy 1: PDF bookmarks
# ----------------------------------------------------------------------

def _split_by_bookmarks(pdf_path: str, fulltext: str) -> list[Chapter] | None:
    """Walk the PDF outline and slice fulltext by page ranges.

    Returns None if:
    - pdfplumber isn't installed
    - PDF has no outline
    - outline exists but we can't map outline entries to page numbers
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            outline = getattr(pdf, "outline", None) or getattr(
                pdf, "outlines", None
            )
            if not outline:
                return None
            # Extract per-page text so we can slice by page range.
            page_texts = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:
        log.debug("pdfplumber open/extract failed on %s: %s", pdf_path, e)
        return None

    # pdfplumber's outline format varies by version. We handle two
    # common shapes: a flat list of dicts with {title, page_number},
    # or a nested list where each entry is [title, page] or similar.
    flat = _flatten_outline(outline)
    if not flat:
        return None

    # Keep only top-level-looking entries (heuristic: titles that
    # contain "chapter" / Chinese chapter markers or look like a
    # numbered top-level heading). This prevents the outline's
    # section/subsection levels from producing 200 "chapters".
    chapter_entries = [
        (title, page) for title, page in flat if _looks_like_chapter(title)
    ]
    # If heuristic kept nothing, fall back to the full flat list
    # (many non-fiction books have titles like "Introduction", "The
    # Electrical Grid" that don't match the chapter regex).
    if len(chapter_entries) < 2:
        chapter_entries = flat

    if len(chapter_entries) < 2:
        return None

    # Slice page_texts by the start pages. Sort by page first — some
    # outlines are out of order. Tolerate out-of-bounds pages.
    entries_sorted = sorted(
        (
            (t, max(0, min(p, len(page_texts) - 1)))
            for t, p in chapter_entries
            if isinstance(p, int)
        ),
        key=lambda x: x[1],
    )
    if len(entries_sorted) < 2:
        return None

    chapters: list[Chapter] = []
    # Bookmark path has the same garbage-entry problem as regex: many
    # PDFs have bookmarks pointing at "List of Figures", "References",
    # "Index", which are NOT useful as chapter summaries. Apply the
    # same tier-1/tier-2 filters here so book outlines with noisy
    # entries produce clean chapter lists.
    rejected_matter = 0
    rejected_caption = 0
    rejected_short = 0
    rejected_data = 0
    for i, (title, start) in enumerate(entries_sorted):
        end = entries_sorted[i + 1][1] if i + 1 < len(entries_sorted) else len(page_texts)
        text = "\n\n".join(page_texts[start:end]).strip()
        if not text:
            continue
        clean_title = title.strip()
        if _looks_like_front_or_back_matter(clean_title):
            rejected_matter += 1
            continue
        if _looks_like_caption(clean_title):
            rejected_caption += 1
            continue
        if len(text) < MIN_CHAPTER_CHARS:
            rejected_short += 1
            continue
        if _is_data_blob(text):
            rejected_data += 1
            continue
        pages = f"{start + 1}-{end}" if end > start else f"{start + 1}"
        chapters.append(Chapter(
            number=len(chapters) + 1,
            title=clean_title,
            text=text,
            pages=pages,
        ))
    total_rej = (rejected_matter + rejected_caption
                 + rejected_short + rejected_data)
    if total_rej:
        log.info(
            "bookmark split filtered %d candidate(s): "
            "short=%d front/back-matter=%d caption=%d data-blob=%d",
            total_rej, rejected_short, rejected_matter,
            rejected_caption, rejected_data,
        )
    return chapters if chapters else None


def _flatten_outline(outline) -> list[tuple[str, int]]:
    """Walk nested outline structures into a flat [(title, page), ...].

    Handles pdfplumber's common shapes defensively. Unknown entry
    shapes are skipped silently.
    """
    out: list[tuple[str, int]] = []

    def walk(node):
        if isinstance(node, dict):
            title = node.get("title") or node.get("Title")
            page = node.get("page_number") or node.get("page")
            if isinstance(title, str) and isinstance(page, int):
                out.append((title, page))
            for child in node.get("children", []) or []:
                walk(child)
        elif isinstance(node, (list, tuple)):
            if (
                len(node) >= 2
                and isinstance(node[0], str)
                and isinstance(node[1], int)
            ):
                out.append((node[0], node[1]))
            else:
                for child in node:
                    walk(child)

    walk(outline)
    return out


def _looks_like_chapter(title: str) -> bool:
    """Heuristic: is this outline entry a top-level chapter?"""
    if not title:
        return False
    t = title.strip()
    # English / mixed-lead: "Chapter 1", "Ch. 3", "Part 2", or the
    # Chinese character 第 followed by an arabic-numeral chapter ref.
    if re.match(r"^(chapter|ch\.?|part|第)\s*\d+", t, re.IGNORECASE):
        return True
    # Pure Chinese chapter header with either CJK or arabic numeral.
    # The character class covers 一..十 + 百 + 零 (numerals in Chinese
    # chapter numbering) plus 章/部/篇 (chapter/part/volume markers).
    if re.match(r"^第\s*[一二三四五六七八九十百零\d]+\s*[章部篇]", t):
        return True
    if re.match(r"^\d+[\.\s]", t) and len(t) < 80:
        return True
    return False


# ----------------------------------------------------------------------
# Strategy 2: regex on plain text
# ----------------------------------------------------------------------

# Multiple patterns ORed; whichever matches more often wins. We do
# this because a book might use EITHER "Chapter 1" OR "1 Introduction"
# style and we don't want to mix them. The zh_chapter pattern
# matches Chinese chapter headers like "第3章 导论" or
# "第三章 概述"; the CJK character class is required for this
# functionality and is not a localisation residue.
_CHAPTER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("en_chapter",
     re.compile(r"(?m)^(?:Chapter|CHAPTER|Ch\.)\s+(\d+)(?:[:\.\s]+([^\n]+))?$")),
    ("zh_chapter",
     re.compile(r"(?m)^第\s*([一二三四五六七八九十百零\d]+)\s*章\s*([^\n]*)$")),
    ("numbered_heading",
     re.compile(r"(?m)^(\d+)(?:\.)?\s+([A-Z][^\n]{2,80})$")),
]


def _split_by_regex(fulltext: str) -> list[Chapter] | None:
    """Find chapter-header lines and split between them."""
    best: tuple[str, list[re.Match]] | None = None
    for name, pat in _CHAPTER_PATTERNS:
        matches = list(pat.finditer(fulltext))
        # Require at least 2 matches (otherwise nothing to split on)
        # and not too many (hundreds = likely numbered figure captions).
        if 2 <= len(matches) <= MAX_CHAPTERS and (
            best is None or len(matches) > len(best[1])
        ):
            best = (name, matches)

    if not best:
        return None
    _, matches = best

    chapters: list[Chapter] = []
    # Count skipped reasons so the log says WHY regex output shrank.
    # Helps diagnose books where nothing survives filtering (and the
    # pipeline then falls through to LLM or single-chapter fallback).
    rejected_short = 0
    rejected_matter = 0
    rejected_caption = 0
    rejected_data = 0
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(fulltext)
        body = fulltext[start:end].strip()
        header_line = m.group(0).strip()

        # Tier 1 filter: front/back-matter section (preface, index,
        # bibliography, …). Cheap regex check on title.
        if _looks_like_front_or_back_matter(header_line):
            rejected_matter += 1
            continue
        # Tier 1 filter: figure / table / equation caption (never a
        # chapter).
        if _looks_like_caption(header_line):
            rejected_caption += 1
            continue
        # Tier 2 filter: minimum body length (real chapters are 5K+).
        if len(body) < MIN_CHAPTER_CHARS:
            rejected_short += 1
            continue
        # Tier 2 filter: numeric density (data dumps / bibliography
        # columns masquerading as chapters).
        if _is_data_blob(body):
            rejected_data += 1
            continue

        # Use the full header line as the title — simpler than trying
        # to split "Chapter 5" from "5: RMS Measurement".
        chapters.append(Chapter(
            number=len(chapters) + 1,
            title=header_line,
            text=body,
        ))

    total_rejected = (
        rejected_short + rejected_matter
        + rejected_caption + rejected_data
    )
    if total_rejected:
        log.info(
            "regex split filtered %d candidate(s): "
            "short=%d front/back-matter=%d caption=%d data-blob=%d",
            total_rejected, rejected_short, rejected_matter,
            rejected_caption, rejected_data,
        )
    if not chapters:
        return None
    # Health check: if the mean body length is suspiciously low (e.g.
    # we matched a "List of Figures" with chapter-like labels spaced
    # along a single page), reject the whole regex output so the
    # caller can try the next strategy. Real books have multi-
    # thousand-char chapters; a 700-char mean is diagnostic of a
    # false-positive storm. Concrete v22 failure: one thesis produced
    # 50 "chapters" averaging ~800 chars each (TOC-entry windows),
    # all 50 went to the LLM, all 50 returned non-JSON.
    mean_len = sum(len(c.text) for c in chapters) / len(chapters)
    if len(chapters) >= 10 and mean_len < REGEX_SUSPICIOUSLY_SHORT_MEAN:
        log.warning(
            "regex split produced %d chapters with mean length %.0f "
            "chars (<%d threshold). Likely matched a TOC/index listing "
            "rather than real chapter bodies. Rejecting regex result "
            "so LLM / fallback strategy can run.",
            len(chapters), mean_len, REGEX_SUSPICIOUSLY_SHORT_MEAN,
        )
        return None
    return chapters


# ----------------------------------------------------------------------
# Strategy 3: LLM-assisted
# ----------------------------------------------------------------------

_LLM_SPLIT_SYSTEM = """\
你是一个文档结构识别助手。用户给你一本书或学位论文的开头部分 \
(可能是前 30-100 页的纯文本),你的任务是识别出章节结构。

请严格返回以下 JSON,不要加任何其他文字或 markdown 代码块:

{
  "chapters": [
    {"title": "第1章 导论", "start_marker": "第1章 导论"},
    {"title": "Chapter 2 ...", "start_marker": "Chapter 2"},
    ...
  ]
}

规则:
- start_marker 必须是原文中**逐字出现**的一小段文字 (8-40 字符),\
我会用它在全文中定位章节起点。不要改写,不要翻译。
- 只识别顶层章节(chapter / 第 N 章 / Part 等),不要识别 section / subsection。
- 如果看不出明确章节结构,返回 {"chapters": []}。
"""


def _split_by_llm(fulltext: str, provider) -> list[Chapter] | None:
    """Ask an LLM to identify chapter headers, then locate each in
    the fulltext and slice.

    Sends only the first 150K chars (enough for most tables of
    contents + first few chapters) to keep input cost bounded.
    """
    head = fulltext[:150_000]
    user = f"以下是文本的开头部分,请识别章节结构:\n\n---\n{head}\n---"
    try:
        text, _pt, _ct = provider.complete(
            _LLM_SPLIT_SYSTEM, user,
            max_output_tokens=4000,
            temperature=0.1,
        )
    except Exception as e:
        log.debug("LLM split call failed: %s", e)
        return None

    # Strip code fences if present.
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()

    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    raw_chapters = obj.get("chapters")
    if not isinstance(raw_chapters, list) or len(raw_chapters) < 2:
        return None

    # Locate each start_marker in the full text. If a marker is
    # missing or ambiguous (many hits), skip that chapter — better
    # to undercount than to slice wrong.
    entries: list[tuple[str, int]] = []
    for entry in raw_chapters:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        marker = entry.get("start_marker")
        if not isinstance(title, str) or not isinstance(marker, str):
            continue
        marker = marker.strip()
        if len(marker) < 4:
            continue
        idx = fulltext.find(marker)
        if idx < 0:
            # marker 1st try failed — try normalising whitespace
            normalised = re.sub(r"\s+", " ", marker)
            idx = fulltext.find(normalised)
            if idx < 0:
                continue
        # Require the marker to be roughly at a paragraph start: the
        # char before should be a newline (or it's at position 0).
        # This filters out matches that landed inside other sentences.
        if idx > 0 and fulltext[idx - 1] not in ("\n", " "):
            continue
        entries.append((title.strip(), idx))

    if len(entries) < 2:
        return None

    entries.sort(key=lambda x: x[1])

    # Slice.
    chapters: list[Chapter] = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < len(entries) else len(fulltext)
        body = fulltext[start:end].strip()
        # Same tier-1/2 filters as regex/bookmarks paths. The LLM
        # splitter is usually cleaner but has its own failure mode:
        # sometimes hallucinates "References" or "Index" as a chapter
        # title. Apply the same guards so no LLM-produced chapter
        # slips through just because it came from the "smart" path.
        if _looks_like_front_or_back_matter(title):
            continue
        if _looks_like_caption(title):
            continue
        if len(body) < MIN_CHAPTER_CHARS:
            continue
        if _is_data_blob(body):
            continue
        chapters.append(Chapter(
            number=len(chapters) + 1,
            title=title,
            text=body,
        ))
    return chapters if chapters else None


# ----------------------------------------------------------------------
# Post-processing
# ----------------------------------------------------------------------

def _is_plausible(chapters: list[Chapter]) -> bool:
    """Sanity check: does this split look reasonable?

    Reject if:
    - fewer than 2 chapters (not worth the long pipeline)
    - more than MAX_CHAPTERS (almost certainly a false-positive storm)
    - chapters wildly unbalanced (one chapter is >90% of all text →
      splitter missed most breakpoints)
    """
    if len(chapters) < 2 or len(chapters) > MAX_CHAPTERS:
        return False
    total = sum(len(ch.text) for ch in chapters)
    if total == 0:
        return False
    longest = max(len(ch.text) for ch in chapters)
    if longest / total > 0.90:
        return False
    return True


def _apply_chapter_caps(chapters: list[Chapter]) -> list[Chapter]:
    """Enforce MAX_CHAPTER_CHARS by subsection-splitting any chapter
    that exceeds it. Subsections are numbered inside the parent
    chapter (5 → 5.1, 5.2, ...). We split on paragraph boundaries
    (double-newline) so semantic units aren't cut mid-sentence.
    """
    out: list[Chapter] = []
    for ch in chapters:
        if len(ch.text) <= MAX_CHAPTER_CHARS:
            out.append(ch)
            continue
        subs = _subsection_split(ch.text, MAX_CHAPTER_CHARS)
        for i, sub_text in enumerate(subs):
            out.append(Chapter(
                number=len(out) + 1,
                title=f"{ch.title} (part {i + 1}/{len(subs)})",
                text=sub_text,
                pages=ch.pages,
                subsection_of=ch.number,
            ))
    # Renumber from 1 after possible subsection expansion.
    for i, ch in enumerate(out, start=1):
        ch.number = i
    return out


def _subsection_split(text: str, cap: int) -> list[str]:
    """Split on paragraph boundaries, gluing back together until
    each part is close to (but ≤) `cap` chars.
    """
    paras = re.split(r"\n\s*\n", text)
    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for p in paras:
        if not p.strip():
            continue
        p_len = len(p) + 2  # +2 for the \n\n separator
        if current and current_len + p_len > cap:
            out.append("\n\n".join(current))
            current = [p]
            current_len = p_len
        else:
            current.append(p)
            current_len += p_len
    if current:
        out.append("\n\n".join(current))
    # Edge case: a single paragraph longer than cap (rare, but
    # happens with PDFs that lost paragraph breaks). Hard-cut.
    final: list[str] = []
    for part in out:
        if len(part) <= cap:
            final.append(part)
        else:
            for i in range(0, len(part), cap):
                final.append(part[i:i + cap])
    return final or [text[:cap]]
