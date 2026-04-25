"""Long-form pipeline: for books and theses, split into chapters,
generate a per-chapter map via LLM, and write each chapter as a
SIBLING paper md alongside the parent (v26 data model).

Flow (v26):

  1. split_into_chapters(fulltext, pdf_path)     (→ longform_split.py)
  2. for each chapter: LLM → chapter-map JSON → paper md at
     `papers/<KEY>-chNN.md` (kind=paper, zotero_key=<KEY>)
  3. parent paper md: fulltext region gets a chapter index table
     pointing at the chapter sibling mds

v25 → v26 data-model change: in v25, per-chapter outputs were
written to `thoughts/<date>-<key>-ch<NN>-*.md` (kind=thought). In
v26 they become full papers at `papers/<KEY>-chNN.md`, sharing the
parent Zotero key. This makes book chapters first-class papers —
searchable, linkable, and individually re-summarizable — instead
of being awkwardly classified as thoughts. See
kb_mcp.paths.is_book_chapter_filename for the convention.

No kb_write audit/git-per-chapter — we batch-write directly via
atomic_write and the caller commits the whole ingest (15 chapters =
15 files changed = 1 commit "longform ingest: <paper_key>").
Rationale: chapter papers are machine-generated in bulk; going
through kb_write would produce N audit entries and N git commits per
book, swamping the real (user-generated) signal in both.

Chapter paper frontmatter (v26):

  kind: paper
  zotero_key: <BOOKKEY>              # SHARED with parent
  title: "<book title> — Chapter N: <chapter title>"
  authors: [...]                     # copied from parent
  year: <int>                        # copied from parent
  item_type: book_chapter            # distinguishes chapter from whole-book
  doi: ""                            # chapter has no own DOI
  source_paper: papers/<BOOKKEY>     # explicit parent pointer
  source_chapter: 5
  source_pages: "142-167"
  kb_refs: [papers/<BOOKKEY>]        # graph-layer back-link
  fulltext_source: pdf
  fulltext_processed: true

Filename: `papers/<BOOKKEY>-ch<NN>.md` — NO date prefix (v25 used
a dated thought slug). Zero-padded 2 digits for chapter number so
the filesystem ordering matches chapter order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


log = logging.getLogger(__name__)


# Per-chapter LLM template. Deliberately different from paper summary:
# fewer sections, no "significance for my research" (that's a
# whole-book judgment, deferred to stage 3), more emphasis on
# extraction (concepts, equations, methods) for retrieval.
CHAPTER_SYSTEM_PROMPT = """\
你是一位电力电子/电力系统领域的研究助理。用户给你一本书或学位论文的\
某一章内容,请按固定 6 节结构产出中文摘要,以 JSON 返回。

规则:
- 每节 2-6 句或 3-8 条 bullet,简洁具体。
- 保留英文术语和公式的原始形式(LaTeX 数学可以直接保留 $...$)。
- 不臆造。原文没有的内容不写。
- 如果某节内容确实不适用(例如理论章节没有实验),写 "(本章无)"。

严格按以下 JSON 返回,不要包裹 code fence,不要加任何其他文字:

{
  "overview": "章节概览 (2-4 句)",
  "concepts": "核心概念 (3-8 条, 每条一行 markdown bullet)",
  "equations": "定理/公式 (3-8 条, 含编号和简短说明, 保留 LaTeX)",
  "methods": "方法/实验 (3-8 条 或 2-4 句)",
  "conclusions": "关键结论 (3-6 条 bullet)",
  "citations": "本章内的重要引文/参考 (3-8 条, 可引用他们的观点)"
}
"""


CHAPTER_USER_TMPL = """\
书/论文标题: {title}
作者: {authors}
年份: {year}

章节 {chapter_num}: {chapter_title}
{pages_line}

---
{chapter_text}
---

请返回 6 节 JSON。
"""


# Sections emitted in a chapter thought body, in fixed order.
CHAPTER_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("overview", "章节概览"),
    ("concepts", "核心概念"),
    ("equations", "定理/公式"),
    ("methods", "方法/实验"),
    ("conclusions", "关键结论"),
    ("citations", "引文定位"),
)


@dataclass
class ChapterOutcome:
    """Per-chapter result: where it was written, token usage."""

    number: int
    title: str
    thought_slug: str
    thought_path: Path
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class LongformOutcome:
    """Aggregate result for one book / thesis ingest."""

    paper_key: str
    chapters: list                   # list[longform_split.Chapter]
    chapters_written: int
    split_source: str                # bookmarks | regex | llm | ...
    per_chapter: list = field(default_factory=list)  # list[ChapterOutcome]
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LongformError(Exception):
    """Long-form ingest failure that the caller should treat as a
    per-paper fail (count as llm-fail, continue with other papers).
    """


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def longform_ingest_paper(
    *,
    cfg,                             # kb_importer.config.Config
    paper_key: str,
    paper,                           # kb_importer.zotero_reader.ZoteroItem
    fulltext: str,
    pdf_path: str | None = None,
    provider,                        # kb_importer.summarize.LLMProvider
    max_output_tokens: int = 8000,
    dryrun: bool = False,
    force_regenerate: bool = False,
) -> LongformOutcome:
    """Stage 1 + 2 of the long-form pipeline.

    Stage 1: split_into_chapters (local; may call LLM for hard cases).
    Stage 2: per-chapter LLM map → thought md writes.

    In dryrun mode, runs only stage 1 and returns outcome with
    chapters populated but chapters_written=0, no thought files.

    Idempotency (v24): if `thoughts/*-<paper_key_lower>-ch*.md`
    already exists on disk, skip the whole ingest unless
    force_regenerate=True. Without this guard, running `--fulltext`
    twice (e.g. on the same KB from two machines) would produce two
    parallel sets of chapter thoughts with slightly different slugs —
    the LLM's chapter titles vary run-to-run, so filename collisions
    don't prevent duplicates. See v22 cross-platform protocol report.

    Raises LongformError on anything the caller should count as a
    per-paper fail. Transient / per-chapter LLM failures accumulate
    into skipped chapters rather than aborting the whole book.

    After stage 2 finishes successfully, the parent paper's md gets
    its fulltext region replaced with a chapter index table via
    _write_parent_chapter_index. fulltext_processed is set to true
    at that point — one successful book = one atomic completion flag
    on the parent paper.
    """
    from .longform_split import split_into_chapters, SplitError
    from .md_builder import paper_md_path

    # Idempotency check — before any LLM spend.
    # v26: book chapters now live at papers/<KEY>-chNN.md (same kind
    # as any paper, sharing zotero_key with the parent book). Scan
    # there. We also check the v25 location (thoughts/*-<key>-ch*.md)
    # but those are considered obsolete data the user needs to
    # reorganise; presence there does NOT count as "already processed"
    # because we want v26 to produce the v26 layout.
    papers_dir = cfg.kb_root / "papers"
    if papers_dir.is_dir() and not force_regenerate:
        existing = list(papers_dir.glob(f"{paper_key}-ch*.md"))
        if existing:
            log.info(
                "longform skip %s: %d existing chapter md(s) at "
                "papers/%s-ch*.md (pass force_regenerate=True to override)",
                paper_key, len(existing), paper_key,
            )
            return LongformOutcome(
                paper_key=paper_key,
                chapters=[],
                chapters_written=0,
                split_source="skipped_idempotent",
            )

    # Stage 1: split
    try:
        split = split_into_chapters(
            fulltext,
            pdf_path=pdf_path,
            allow_llm_fallback=(not dryrun),
            llm_provider=(provider if not dryrun else None),
        )
    except SplitError as e:
        raise LongformError(f"split failed: {e}") from e

    outcome = LongformOutcome(
        paper_key=paper_key,
        chapters=split.chapters,
        chapters_written=0,
        split_source=split.source,
    )

    if dryrun:
        return outcome

    # Stage 2: per-chapter map + thought write
    paper_title = paper.title or paper_key
    authors_s = ", ".join(paper.authors or []) or "(unknown)"
    today = date.today().isoformat()

    for ch in split.chapters:
        try:
            co = _process_chapter(
                cfg=cfg,
                paper_key=paper_key,
                paper_title=paper_title,
                paper_authors=authors_s,
                paper_year=str(paper.year or "unknown"),
                chapter=ch,
                provider=provider,
                max_output_tokens=max_output_tokens,
                date_iso=today,
            )
        except Exception as e:
            log.warning(
                "chapter %s of %s failed, skipping: %s",
                ch.number, paper_key, e,
            )
            continue
        outcome.per_chapter.append(co)
        outcome.chapters_written += 1
        outcome.prompt_tokens += co.prompt_tokens
        outcome.completion_tokens += co.completion_tokens

    if outcome.chapters_written == 0:
        raise LongformError(
            f"all {len(split.chapters)} chapters failed"
        )

    # Stage 2.5: update parent paper md with chapter index.
    # We go directly through inject_fulltext (not writeback_summary)
    # because we need to record an additional `longform_split_source`
    # field — keeping fulltext_source as a flat enum (zotero_api /
    # pdfplumber / pypdf / longform) and putting the split mechanism
    # (bookmarks / regex / llm / fallback) in its own field keeps
    # either field cleanly queryable from frontmatter consumers.
    from datetime import datetime, timezone
    from .fulltext import SOURCE_LONGFORM
    from .md_io import inject_fulltext

    parent_md = paper_md_path(cfg.kb_root, paper_key)
    index_md = _render_chapter_index(paper_key, outcome)
    try:
        inject_fulltext(
            parent_md,
            fulltext_body=index_md,
            source_meta={
                "fulltext_processed": True,
                "fulltext_source": SOURCE_LONGFORM,
                "fulltext_extracted_at": (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                ),
                "fulltext_model": f"{provider.name}/{provider.model}",
                "longform_split_source": split.source,
                "longform_chapters_written": outcome.chapters_written,
            },
        )
    except Exception as e:
        # Chapters are on disk regardless — the parent index is
        # nice-to-have but not catastrophic to miss. Warn and continue.
        log.warning(
            "parent chapter index writeback failed for %s: %s",
            paper_key, e,
        )

    return outcome


# ----------------------------------------------------------------------
# Per-chapter processing
# ----------------------------------------------------------------------

def _process_chapter(
    *,
    cfg,
    paper_key: str,
    paper_title: str,
    paper_authors: str,
    paper_year: str,
    chapter,                 # longform_split.Chapter
    provider,
    max_output_tokens: int,
    date_iso: str,
) -> ChapterOutcome:
    """Generate chapter map via LLM, write thought md, return outcome."""
    pages_line = f"(pages {chapter.pages})" if chapter.pages else ""
    user = CHAPTER_USER_TMPL.format(
        title=paper_title,
        authors=paper_authors,
        year=paper_year,
        chapter_num=chapter.number,
        chapter_title=chapter.title,
        pages_line=pages_line,
        chapter_text=chapter.text,
    )

    text, pt, ct = provider.complete(
        CHAPTER_SYSTEM_PROMPT, user,
        max_output_tokens=max_output_tokens,
        temperature=0.2,
    )
    sections = _parse_chapter_sections(text)
    if sections is _PLACEHOLDER_ONLY:
        # Valid JSON, right shape, but the LLM filled every field with
        # the N/A placeholder — signals the input wasn't a real chapter
        # (figure caption, y-axis label, single-line TOC entry that
        # slipped past the splitter's MIN_CHAPTER_CHARS filter). Retry
        # will produce the same output, so skip immediately.
        raise LongformError(
            f"chapter {chapter.number} LLM returned all-placeholder "
            f"sections (not a real chapter body); skipped without retry"
        )
    if sections is None:
        # v24 fail-fast: if the first response doesn't even begin with
        # a JSON opener, it's almost never a transient formatting
        # hiccup — the model has decided this "chapter" isn't a real
        # chapter (common for garbage slices the splitter should have
        # filtered: TOC pages, figure captions, bibliography columns).
        # Retrying wastes the full input tokens again and almost
        # always fails identically. Bail immediately.
        #
        # Why not just check `text.startswith("{")` (fast path for
        # code-fence wrapped JSON)? Because the LLM sometimes emits
        # ```json\n{...\n```. `_parse_chapter_sections` already
        # strips those fences; if it still returns None, genuine
        # non-JSON. So we check the stripped-of-whitespace opener.
        stripped = (text or "").lstrip()
        first_char_is_json_opener = (
            stripped.startswith("{")
            or stripped.startswith("```")  # fenced, parser will unfence
        )
        if not first_char_is_json_opener:
            log.info(
                "chapter %d: first LLM response non-JSON "
                "(opener %r); skipping retry to save API budget "
                "(v22 report Tier-3 fail-fast).",
                chapter.number, stripped[:40],
            )
            raise LongformError(
                f"chapter {chapter.number} LLM returned non-JSON "
                f"on first try and response opener suggests a "
                f"garbage slice rather than a format bug; skipped "
                f"retry"
            )
        # Opener looked JSON-ish — worth one retry with an explicit
        # JSON-only nudge (e.g. trailing text broke parse).
        retry_user = (
            user + "\n\n(Note: return only the JSON object described; "
            "no markdown fences, no prefix.)"
        )
        text2, pt2, ct2 = provider.complete(
            CHAPTER_SYSTEM_PROMPT, retry_user,
            max_output_tokens=max_output_tokens,
            temperature=0.2,
        )
        pt += pt2
        ct += ct2
        sections = _parse_chapter_sections(text2)
        if sections is _PLACEHOLDER_ONLY:
            raise LongformError(
                f"chapter {chapter.number} LLM returned all-placeholder "
                f"sections on retry (not a real chapter body); skipped"
            )
        if sections is None:
            raise LongformError(
                f"chapter {chapter.number} LLM returned non-JSON twice"
            )

    body = _render_chapter_body(chapter, sections, paper_key, paper_title)
    slug, thought_path = _write_chapter_paper(
        cfg=cfg,
        paper_key=paper_key,
        paper_title=paper_title,
        chapter=chapter,
        body=body,
        date_iso=date_iso,
    )

    return ChapterOutcome(
        number=chapter.number,
        title=chapter.title,
        thought_slug=slug,
        thought_path=thought_path,
        prompt_tokens=pt,
        completion_tokens=ct,
    )


class _PlaceholderOnly:
    """Sentinel returned by _parse_chapter_sections when the JSON
    parsed fine but the content is almost all N/A placeholder.
    Caller treats this as "don't retry, this isn't a real chapter"
    rather than "retry with a JSON nudge".
    """
    __slots__ = ()


_PLACEHOLDER_ONLY = _PlaceholderOnly()


def _parse_chapter_sections(text: str):
    """Parse chapter-map JSON. Returns one of:
      - dict mapping section key → body (happy path, write thought)
      - _PLACEHOLDER_ONLY sentinel (valid JSON but mostly "本章无")
      - None (JSON parse error / too few sections / not a dict)

    The distinction matters because the caller retries on None (format
    hiccups respond to a "just JSON please" nudge) but does NOT retry
    on _PLACEHOLDER_ONLY (the input wasn't a real chapter — retry will
    almost always produce the same placeholder output, just wasting
    another round of tokens).

    Gates:
      1. Parseable JSON, is a dict → else None
      2. At least 4 of 6 sections populated with non-empty strings
         (2/3 threshold, consistent with short-pipeline's 5-of-7 rule)
         → else None
      3. At least 3 sections have *substantive* content (not the
         prompt-mandated "本章无" / "本章没有" N/A placeholder) → else
         _PLACEHOLDER_ONLY
    """
    import json
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
    out: dict = {}
    for key, _label in CHAPTER_SECTION_ORDER:
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    if len(out) < 4:
        return None

    # Gate 3 (placeholder-only check).
    substantive = 0
    for v in out.values():
        if not _is_placeholder_value(v):
            substantive += 1
    if substantive < 3:
        log.info(
            "chapter section parse: %d/%d substantive, %d placeholders. "
            "Treating as non-chapter (likely figure caption / y-axis "
            "label / page fragment) and skipping without retry.",
            substantive, len(out), len(out) - substantive,
        )
        return _PLACEHOLDER_ONLY
    return out


# Placeholder strings the chapter prompt explicitly authorises when a
# section is genuinely N/A for a chapter (see CHAPTER_SYSTEM_PROMPT
# line "如果某节内容确实不适用, 写 '(本章无)'"). We match these liberally
# to catch minor LLM variations ("本章无内容", "本章没有", wrapped in
# asterisks, etc). The goal is not semantic analysis — just detecting
# the shape of "I have nothing to say here" responses.
_PLACEHOLDER_SUBSTRINGS = (
    "本章无",     # "本章无", "本章无内容", "(本章无)"
    "本章没有",    # "本章没有", "本章没有涉及"
    "本节无",
    "本节没有",
    "无相关",
    "n/a",
    "not applicable",
)


def _is_placeholder_value(v: str) -> bool:
    """True if the section body appears to be a prompt-mandated N/A
    placeholder rather than substantive content. Conservative: a
    real chapter section that happens to mention "本章无" mid-sentence
    won't match because we require the placeholder to dominate the
    whole (short) entry, not just appear.
    """
    s = v.strip().strip("*").strip("()（）").strip().lower()
    # Short entries (< 30 chars) that contain any placeholder string
    # are considered pure placeholders — real chapter notes aren't
    # this terse even for edge-case sections.
    if len(s) < 30:
        for p in _PLACEHOLDER_SUBSTRINGS:
            if p in s:
                return True
    return False


def _render_chapter_body(
    chapter, sections: dict, paper_key: str, paper_title: str,
) -> str:
    """Render the chapter map as thought body markdown."""
    lines: list[str] = []
    lines.append(f"*From [[papers/{paper_key}|{paper_title}]], "
                 f"chapter {chapter.number}"
                 f"{' (pages ' + chapter.pages + ')' if chapter.pages else ''}.*")
    lines.append("")
    for key, label in CHAPTER_SECTION_ORDER:
        lines.append(f"## {label}")
        lines.append("")
        lines.append(sections.get(key, "*(本章无)*"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_chapter_paper(
    *,
    cfg,
    paper_key: str,
    paper_title: str,
    chapter,
    body: str,
    date_iso: str,
) -> tuple[str, Path]:
    """Write one chapter as a paper md via atomic_write.

    v26: chapters live at papers/<PARENT>-chNN.md, kind=paper,
    sharing zotero_key with the parent book. Filename is deterministic
    so re-running longform on the same book yields the same paths
    (idempotency). The chapter body is wrapped in the same kb-fulltext
    markers a regular paper uses, so the indexer's fulltext extractor
    finds it and search returns chapter-level hits.

    Returns (chapter_paper_key, md_path). `chapter_paper_key` is the
    chapter's own paper_key (e.g. "BOOKKEY-ch03"), used by the caller
    to build the parent's chapter-index table.
    """
    from kb_write.atomic import atomic_write
    from .md_io import FULLTEXT_START, FULLTEXT_END

    papers_dir = cfg.kb_root / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    chapter_key = f"{paper_key}-ch{chapter.number:02d}"
    md_path = papers_dir / f"{chapter_key}.md"

    # AI zone markers come from kb_write's zones module when available,
    # else fall back to the literal strings (kb_importer may be run
    # without kb_write installed — soft dependency).
    try:
        from kb_write.zones import AI_ZONE_START, AI_ZONE_END
    except ImportError:
        AI_ZONE_START = "<!-- kb-ai-zone-start -->"
        AI_ZONE_END = "<!-- kb-ai-zone-end -->"

    # Frontmatter. kind=paper; zotero_key shared with the parent;
    # item_type flags it as a chapter so consumers can treat it
    # slightly differently (e.g. omit from citation-count refresh).
    #
    # 1.4.2: build via yaml.safe_dump rather than hand-concatenating
    # f-strings. Pre-1.4.2 only `title` went through _yaml_escape;
    # zotero_key / parent_paper / chapter_number / source_pages /
    # date were inserted directly. Today's Zotero keys are
    # [A-Z0-9]{8} so safe — but a future Zotero schema change, or a
    # chapter title containing `\n` / `"` / `:` would silently
    # produce malformed YAML and the next read would yield None
    # frontmatter, making the chapter look "unprocessed" forever
    # and burning LLM tokens on repeat. safe_dump handles all of
    # this correctly by construction.
    import yaml as _yaml
    _fm = {
        "kind": "paper",
        "zotero_key": paper_key,
        "item_type": "book_chapter",
        "title": (
            f"{paper_title} — Chapter {chapter.number}: "
            f"{chapter.title}"
        ),
        "chapter_number": chapter.number,
        "parent_paper": f"papers/{paper_key}",
    }
    if chapter.pages:
        _fm["source_pages"] = chapter.pages
    _fm["fulltext_processed"] = True
    _fm["fulltext_source"] = "llm_longform"
    _fm["kb_refs"] = [f"papers/{paper_key}"]
    _fm["kb_tags"] = ["longform", "chapter"]
    _fm["longform_generated_at"] = date_iso

    fm_block = _yaml.safe_dump(
        _fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip()
    fm_lines = ["---", fm_block, "---"]

    # Body: chapter-title heading, then the LLM-generated content
    # wrapped inside kb-fulltext markers (so it's found by the
    # indexer's fulltext extractor), then an empty AI zone so
    # readers can append notes with `kb-write ai-zone append`.
    body_lines = [
        "",
        f"# {paper_title} — Chapter {chapter.number}: {chapter.title}",
        "",
        "## AI Summary (from Full Text)",
        "",
        FULLTEXT_START,
        body.strip(),
        FULLTEXT_END,
        "",
        "---",
        "",
        AI_ZONE_START,
        "",
        AI_ZONE_END,
        "",
    ]

    content = "\n".join(fm_lines) + "\n".join(body_lines)
    atomic_write(md_path, content)
    return chapter_key, md_path


def _render_chapter_index(paper_key: str, outcome: LongformOutcome) -> str:
    """Render the body that goes into the parent paper's fulltext
    region: one table row per chapter, pointing at the thought md.

    Deliberately terse — the real content lives in the thought files.
    This index is a landing page so the parent paper md stays useful
    when something wiki-links to `papers/ABCD1234`.
    """
    lines: list[str] = []
    lines.append("## 章节索引 (长文模式)")
    lines.append("")
    lines.append(
        f"本文献采用长文模式处理:每章单独生成一个 paper md"
        f"(作为本文献的兄弟条目,共享 Zotero key)。共 "
        f"{outcome.chapters_written} 章,切章方法: "
        f"`{outcome.split_source}`。"
    )
    lines.append("")
    lines.append("| # | 章节 | 页 | 章节 md |")
    lines.append("|---|---|---|---|")
    for co in outcome.per_chapter:
        # Pages column: the Chapter dataclass carries pages; look up.
        pages = ""
        for ch in outcome.chapters:
            if ch.number == co.number:
                pages = ch.pages or ""
                break
        # Title may contain `|`; replace to not break the table row.
        safe_title = (co.title or "").replace("|", "\\|")[:60]
        # v26: chapter md lives at papers/<PARENT>-chNN.md. The
        # `thought_slug` field in per_chapter is now the chapter's
        # paper_key (e.g. "BOOKKEY-ch03"), kept under its old name
        # for compatibility with callers that haven't been renamed.
        lines.append(
            f"| {co.number} | {safe_title} | {pages} | "
            f"[[papers/{co.thought_slug}]] |"
        )
    lines.append("")
    lines.append(
        "> 下一步:运行 `kb-mcp index` 把新章节纳入向量索引。"
        f"检索时命中 `papers/{paper_key}-ch*` 的结果即来自本文献的"
        "具体章节。`find_paper_by_key` 按父 key 查返回本页(整书);"
        f"`list_paper_parts('{paper_key}')` 列出全部章节。"
    )
    return "\n".join(lines) + "\n"
