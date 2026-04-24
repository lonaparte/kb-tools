"""Candidate pool sources for `kb-write re-read`.

Two built-in sources:

  `papers`   — all paper mds under papers/*.md (DEFAULT). Returns
               a PaperInfo per md. This is the sound choice: every
               returned paper has a md kb-write knows how to
               re-summarize into.

  `storage`  — attachment dirs under zotero_storage. Walks the
               storage tree, but maps attachments back to paper_keys
               via frontmatter `zotero_attachment_keys`. Only yields
               paper_keys that ALSO have an imported md (otherwise
               re-read has nothing to write to).

               Effectively a permutation of the `papers` pool
               ordered by PDF-on-disk presence — useful as a sanity
               bound when you only want to re-read papers you can
               actually locate the PDF for.

Adding a new source later = one function, one registry entry.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..selectors.base import PaperInfo


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# source: papers/
# ---------------------------------------------------------------------

def source_papers(kb_root: Path) -> list[PaperInfo]:
    """Return a PaperInfo for every md under papers/."""
    papers_dir = kb_root / "papers"
    if not papers_dir.is_dir():
        return []
    infos: list[PaperInfo] = []
    for md in sorted(papers_dir.glob("*.md")):
        if md.name.startswith("."):
            continue
        try:
            info = _build_info(kb_root, md)
        except OSError as e:
            log.warning("re_read_sources: skipping %s (%s)", md.name, e)
            continue
        if info is not None:
            infos.append(info)
    return infos


# ---------------------------------------------------------------------
# source: zotero storage (PDF-on-disk → paper_key)
# ---------------------------------------------------------------------

def source_storage(kb_root: Path, storage_dir: Path) -> list[PaperInfo]:
    """Return PaperInfo for papers that (a) have a PDF under
    storage_dir and (b) have an imported md.

    Walks every <storage>/<ATTKEY>/*.pdf then looks up which paper_key
    references that ATTKEY in frontmatter. Degrades gracefully when
    storage_dir is missing or empty.
    """
    if not storage_dir.is_dir():
        log.warning(
            "re_read_sources: storage dir %s does not exist", storage_dir,
        )
        return []

    papers = source_papers(kb_root)  # all imported mds
    # Build a reverse index: attachment_key → paper_key for imported papers.
    att_to_paper: dict[str, str] = {}
    for p in papers:
        for att in p.zotero_attachment_keys:
            att_to_paper.setdefault(att, p.paper_key)

    # Find attachment keys that have a PDF on disk.
    # 0.29.1: PDFs are only under <storage>/<KEY>/*.pdf now (the
    # _archived/ fallback was removed in state.py). Still skip
    # dot-hidden and any legacy "_archived" folder the user may not
    # have cleaned up yet.
    present_attachments: set[str] = set()
    for att_dir in storage_dir.iterdir():
        if not att_dir.is_dir():
            continue
        if att_dir.name.startswith(".") or att_dir.name == "_archived":
            continue
        if any(att_dir.glob("*.pdf")):
            present_attachments.add(att_dir.name)

    # Map to paper_keys and filter.
    present_paper_keys = {
        att_to_paper[a] for a in present_attachments
        if a in att_to_paper
    }
    return [p for p in papers if p.paper_key in present_paper_keys]


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

SOURCES = {
    "papers":  "All paper mds under papers/*.md (default).",
    "storage": "Papers whose PDF exists under zotero_storage/ AND have an imported md.",
}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

_FM_KIND_RE = re.compile(r'^kind:\s*["\']?paper["\']?\s*$', re.MULTILINE)
_FM_FULLTEXT_RE = re.compile(
    r'^fulltext_processed:\s*["\']?(true|false|yes|no|on|off|1|0)["\']?\s*$',
    re.MULTILINE | re.IGNORECASE,
)
_FM_YEAR_RE = re.compile(r'^year:\s*(\d+)\s*$', re.MULTILINE)
_FM_TITLE_RE = re.compile(r'^title:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
_FM_ITEM_TYPE_RE = re.compile(
    r'^item_type:\s*["\']?([A-Za-z_]+)["\']?\s*$', re.MULTILINE,
)


def _build_info(kb_root: Path, md: Path) -> PaperInfo | None:
    """Parse frontmatter (lightweight) + file stat into a PaperInfo.

    Returns None if the md isn't a kind=paper file.
    """
    st = md.stat()
    # Read top ~8KB; enough for almost all frontmatter blocks.
    with open(md, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(8192)
    if not head.startswith("---\n"):
        return None
    end = head.find("\n---\n", 4)
    if end < 0:
        return None
    fm = head[4:end]

    if not _FM_KIND_RE.search(fm):
        return None

    ft = None
    m = _FM_FULLTEXT_RE.search(fm)
    if m:
        v = m.group(1).lower()
        ft = v in ("true", "yes", "on", "1")

    # v27 fix: frontmatter list parsing now lives in
    # kb_core.frontmatter.extract_list and handles BOTH flow-form
    # (`key: [a, b]`) AND block-form (`key:\n- a\n- b\n`). Prior
    # versions used a flow-only regex, which missed every real md
    # (kb-importer writes block-form by default via PyYAML) —
    # `re-read --source storage` returned 0 results on any
    # realistic library.
    from kb_core.frontmatter import extract_list as _fm_list
    att_tuple = tuple(_fm_list(fm, "zotero_attachment_keys"))
    tag_tuple = tuple(_fm_list(fm, "kb_tags"))

    year = None
    m = _FM_YEAR_RE.search(fm)
    if m:
        try:
            year = int(m.group(1))
        except ValueError:
            pass

    title = None
    m = _FM_TITLE_RE.search(fm)
    if m:
        title = m.group(1).strip()

    item_type = None
    m = _FM_ITEM_TYPE_RE.search(fm)
    if m:
        item_type = m.group(1).strip()

    return PaperInfo(
        paper_key=md.stem,
        md_path=md.relative_to(kb_root).as_posix(),
        md_mtime=st.st_mtime,
        fulltext_processed=ft,
        zotero_attachment_keys=att_tuple,
        kb_tags=tag_tuple,
        year=year,
        title=title,
        item_type=item_type,
    )
