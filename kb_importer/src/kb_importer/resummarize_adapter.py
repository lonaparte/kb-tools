"""Adapter: re-summarize entry points for kb_write.

kb_write.ops.re_summarize is the user-facing command; it delegates
the heavy lifting (PDF extract + LLM calls) to this module so that
kb_write itself doesn't hard-depend on kb_importer's LLM + PDF
machinery. The adapter lives in kb_importer (which already has those
dependencies) and exposes two functions kb_write calls:

  - run_new_summary(kb_root, paper_key, old_md_text, provider, model)
      → list[str] (7 section bodies)
  - judge_sections(kb_root, paper_key, pairs, provider, model)
      → list[(idx, verdict, reason, new_content)]

Both are single-paper operations (one-at-a-time, consistent with
re-summarize semantics). They read provider/model config from the
standard kb-importer config file, allowing overrides per call.

Inputs / outputs are plain lists and tuples — no kb_importer objects
leak through. That way kb_write remains decoupled.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path


log = logging.getLogger(__name__)


# The 7-section structure re-summarize assumes. Indices are
# 1-based to match `## N.` markdown headings; we store the Chinese
# titles kb-importer uses so the new summary lines up.
SECTION_TITLES_CH = [
    "1. 论文的主要内容",
    "2. 核心技术",
    "3. 关键公式与结论",
    "4. 应用场景与局限",
    "5. 与相关工作的对比",
    "6. 启示与可借鉴之处",
    "7. 引文定位",
]


def run_new_summary(
    kb_root: Path,
    paper_key: str,
    old_md_text: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[str]:
    """Run a fresh LLM pass over the paper's PDF, return 7 section bodies.

    Uses the same short-pipeline prompt kb-importer uses for initial
    fulltext summarization, so format/section headings match what the
    old summary has. Output is a list of 7 strings, each starting with
    its `## N. <title>` heading line.

    Raises FileNotFoundError if the PDF can't be located.
    Raises SummarizerError for any LLM-level failure (passed through).
    """
    pdf_path = _locate_pdf_for_paper(kb_root, paper_key, old_md_text)

    # Load importer config to get provider/model + API keys.
    from .config import load_config
    cfg = load_config(kb_root=kb_root)

    # Extract PDF text directly via the private PDF-only helper
    # (the public `extract_fulltext` takes a full attachment list +
    # Zotero reader, which is overkill when we already know the PDF
    # path from _locate_pdf_for_paper).
    from .fulltext import _extract_from_pdf, _truncate
    pdf_text, _source = _extract_from_pdf(pdf_path)
    if not pdf_text:
        raise FileNotFoundError(
            f"{paper_key}: PDF at {pdf_path} yielded no extractable "
            f"text (pdfplumber/pypdf both failed or missing)."
        )
    pdf_text = _truncate(pdf_text)

    # Build an LLM provider (same as kb-importer --fulltext uses).
    from .summarize import build_provider_from_env, summarize_paper
    cfg_fulltext = dict(cfg.fulltext or {})
    prov_name = provider or cfg_fulltext.get("provider", "gemini")
    prov_model = model or cfg_fulltext.get("model")
    llm = build_provider_from_env(provider=prov_name, model=prov_model)

    # Pull metadata from the old md frontmatter so the LLM has context
    # for the "compare to prior work" section.
    title = _extract_frontmatter_field(old_md_text, "title") or paper_key
    year = _extract_frontmatter_field(old_md_text, "year") or ""
    doi = _extract_frontmatter_field(old_md_text, "doi") or ""
    abstract = _extract_frontmatter_field(old_md_text, "abstract") or ""
    authors_list = _extract_frontmatter_list(old_md_text, "authors")
    authors = ", ".join(authors_list)

    result = summarize_paper(
        provider=llm,
        fulltext=pdf_text,
        title=title,
        authors=authors,
        year=year,
        doi=doi,
        abstract=abstract,
    )
    # `result.sections` is {1: text, 2: text, ..., 7: text}. Rebuild
    # the 7 section bodies INCLUDING the headings, matching what
    # _split_into_sections produces from disk.
    out: list[str] = []
    for i in range(1, 8):
        body = (result.sections.get(i) or "").strip()
        title_i = SECTION_TITLES_CH[i - 1]
        out.append(f"## {title_i}\n\n{body}")
    return out


def judge_sections(
    kb_root: Path,
    paper_key: str,
    pairs: list[tuple[int, str, str]],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[tuple[int, str, str, str]]:
    """Compare each (old, new) pair section-by-section, return a verdict.

    Args:
        pairs: list of (section_idx_0based, old_text, new_text).

    Returns a list of tuples (idx, verdict, reason, new_content):
      - verdict ∈ {"new", "old", "tied"}
      - reason: one-line LLM rationale (may be empty)
      - new_content: when verdict="new", the exact replacement body
        that should go into the md; otherwise empty string.

    Implementation: one compact LLM prompt per paper (all pending
    sections in one call) minimises tokens and latency compared
    with per-section prompts.
    """
    if not pairs:
        return []

    from .config import load_config
    cfg = load_config(kb_root=kb_root)
    # Use the same provider/model as run_new_summary unless
    # overridden.
    if provider or model:
        from dataclasses import replace
        override: dict = {}
        if provider:
            override["fulltext"] = dict(cfg.fulltext or {})
            override["fulltext"]["provider"] = provider
        if model:
            override.setdefault("fulltext", dict(cfg.fulltext or {}))
            override["fulltext"]["model"] = model
        cfg = replace(cfg, **override)

    # Build prompt: present each (old, new) section and ask for a
    # JSON verdict array.
    user_parts: list[str] = [
        f"Paper key: {paper_key}",
        "",
        (
            "For each section pair below, decide whether the NEW version "
            "is factually more correct than the OLD version, or whether "
            "the OLD version was already correct. Respond with a JSON "
            "array where each element is "
            '{"section": <1-based int>, "verdict": "new" | "old" | "tied", '
            '"reason": "<one short line>"}. Use "new" only when the new '
            "text fixes a factual error or adds a clearly-correct detail. "
            'Use "old" when the old text was already correct and the new '
            "phrasing added nothing substantive. Use "
            '"tied" when both are essentially equivalent in content."'
        ),
        "",
    ]
    for idx, old, new in pairs:
        user_parts.append(f"--- Section {idx + 1} OLD ---")
        user_parts.append(old.strip())
        user_parts.append("")
        user_parts.append(f"--- Section {idx + 1} NEW ---")
        user_parts.append(new.strip())
        user_parts.append("")

    user_prompt = "\n".join(user_parts)

    # Reuse kb-importer's provider plumbing (same auth / retry /
    # quota-fallback logic that the first-pass pipeline uses).
    from .summarize import build_provider_from_env
    cfg_fulltext = dict(cfg.fulltext or {})
    prov_name = provider or cfg_fulltext.get("provider", "gemini")
    prov_model = model or cfg_fulltext.get("model")
    try:
        llm = build_provider_from_env(provider=prov_name, model=prov_model)
        raw_text, _pt, _ct = llm.complete(
            "You are a careful scientific editor comparing two "
            "versions of the same paper summary. Respond with a JSON "
            "array only — no prose before or after.",
            user_prompt,
            max_output_tokens=4000,
        )
    except Exception as e:
        log.warning(
            "re-summarize: judge LLM call failed (%s); defaulting all "
            "pending sections to verdict='old'", e,
        )
        return [(idx, "old", f"judge-failure: {e}", "") for idx, _, _ in pairs]

    # Strip common ```json wrappers.
    raw = raw_text.strip()
    if raw.startswith("```"):
        # Remove first fence line (maybe ```json) and trailing ```.
        first_nl = raw.find("\n")
        if first_nl >= 0:
            raw = raw[first_nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    # Parse.
    try:
        arr = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        log.warning("re-summarize: judge returned non-JSON; keeping old.")
        return [(idx, "old", "judge non-JSON; kept old", "") for idx, _, _ in pairs]
    if not isinstance(arr, list):
        return [(idx, "old", "judge not list; kept old", "") for idx, _, _ in pairs]

    by_section = {}
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        s = entry.get("section")
        v = entry.get("verdict")
        r = entry.get("reason", "")
        if isinstance(s, int) and v in ("new", "old", "tied"):
            by_section[s] = (v, str(r)[:200])

    out: list[tuple[int, str, str, str]] = []
    for idx, _old, new in pairs:
        sect_num = idx + 1
        v, reason = by_section.get(sect_num, ("old", "no judge entry"))
        new_content = new if v == "new" else ""
        out.append((idx, v, reason, new_content))
    return out


# ---------------------------------------------------------------------
# Helpers (kept private; not part of the adapter contract)
# ---------------------------------------------------------------------

def _locate_pdf_for_paper(
    kb_root: Path, paper_key: str, md_text: str,
) -> Path:
    """Find the PDF attachment for this paper.

    Strategy:
      1. Read `zotero_attachment_keys` from frontmatter; each is an
         attachment key whose PDF lives under `<storage>/<att_key>/*.pdf`.
      2. Walk the first attachment dir; return the first .pdf.
      3. If none found, raise FileNotFoundError.

    Uses kb-importer's storage_dir configuration. For book chapters
    (paper_key like "BOOKKEY-ch03"), we fall back to the parent book's
    md to locate the PDF — the chapter md has no separate attachment.
    """
    # If this is a chapter, look up the parent book's md for the PDF ref.
    base_key = paper_key
    if "-ch" in paper_key:
        base_key = paper_key.split("-ch", 1)[0]
        parent_md = kb_root / "papers" / f"{base_key}.md"
        if parent_md.exists():
            md_text = parent_md.read_text(encoding="utf-8")

    att_keys = _extract_frontmatter_list(md_text, "zotero_attachment_keys")
    if not att_keys:
        raise FileNotFoundError(
            f"{paper_key}: no zotero_attachment_keys in frontmatter — "
            f"cannot locate the PDF."
        )

    from .config import load_config
    cfg = load_config(kb_root=kb_root)
    storage = cfg.zotero_storage_dir
    for att in att_keys:
        att_dir = storage / att
        if not att_dir.is_dir():
            continue
        for pdf in att_dir.glob("*.pdf"):
            return pdf
    raise FileNotFoundError(
        f"{paper_key}: no PDF found under any of its attachment dirs "
        f"({att_keys}). Is zotero_storage_dir correct?"
    )


def _extract_frontmatter_field(md_text: str, key: str) -> str | None:
    """Lightweight frontmatter scan for a scalar field. Returns None
    if not found. Avoids the full frontmatter.load() round-trip."""
    if not md_text.startswith("---\n"):
        return None
    end = md_text.find("\n---\n", 4)
    if end < 0:
        return None
    for line in md_text[4:end].splitlines():
        s = line.strip()
        if s.startswith(f"{key}:"):
            v = s.split(":", 1)[1].strip().strip('"').strip("'")
            return v
    return None


def _extract_frontmatter_list(md_text: str, key: str) -> list[str]:
    """Parse a YAML list scalar (flow-form `key: [a, b, c]` or
    block form) from the frontmatter at the top of `md_text`.

    v27: delegates to kb_core.frontmatter.extract_list so the
    parsing rules (especially block-form indent handling) stay
    in lockstep with kb_write/ops/re_read_sources, which uses
    the same parser. Prior versions had two divergent regex-
    based copies, each with a DIFFERENT bug:

    - This module only matched block items indented `  - ` (2
      spaces). PyYAML's default dump writes `- ` (0 indent), so
      100% of real papers' attachment keys went unparsed, and
      re-summarize refused every paper with "no
      zotero_attachment_keys in frontmatter".
    - re_read_sources matched only the flow form `key: [a, b]`,
      missing the block form entirely.

    Both are fixed by centralising the parser in kb_core.
    """
    if not md_text.startswith("---\n"):
        return []
    end = md_text.find("\n---\n", 4)
    if end < 0:
        return []
    header = md_text[4:end]
    from kb_core.frontmatter import extract_list
    return extract_list(header, key)
