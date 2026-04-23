"""Resolve an external DOI (or title) back to your KB's Zotero key.

The core operation we need for edge-building:

    "paper A (in my KB, key=ABCD1234) cites DOI 10.1109/xxx"
    → is there a paper in my KB with DOI 10.1109/xxx?
    → if yes, build an edge (ABCD1234) -> (that local key).

The resolver builds an in-memory index:
    { doi → paper_key, title_normalized → paper_key }
once per run, then answers lookups O(1).

We don't hit kb-mcp's SQLite for this — we read the papers/*.md
frontmatter directly. Reasons:
  - kb-citations is a separate package and shouldn't depend on
    kb-mcp being indexed first.
  - Frontmatter reading is fast (< 5s for 1200 papers).
  - SQLite might be stale if user just added papers.

Title matching is a fallback (when a reference has no DOI). We
normalize both sides (lowercase, strip punctuation, collapse
whitespace) to avoid false negatives from minor formatting
variance. False positives are possible but rare for academic
titles.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from .provider import normalize_doi


log = logging.getLogger(__name__)


@dataclass
class ResolvedPaper:
    """One local paper, with all identifiers we know about it."""
    key: str                # Zotero key, 8-char alphanumeric
    doi: str | None         # normalized lowercase
    title: str | None
    md_path: Path


class LocalResolver:
    """Build an index of local papers and look up matches.

    Usage:
        r = LocalResolver.from_kb(kb_root)
        key = r.resolve_by_doi("10.1109/xxx")         # returns "ABCD1234" or None
        key = r.resolve_by_title("A Paper on X")       # fallback
    """

    def __init__(self, papers: list[ResolvedPaper]):
        self._papers = papers
        self._by_doi: dict[str, str] = {}
        self._by_title: dict[str, str] = {}
        for p in papers:
            if p.doi:
                # Later-added paper with same DOI would collide; take
                # the first and warn.
                if p.doi in self._by_doi:
                    log.warning(
                        "duplicate DOI %s in KB (keys %s and %s); "
                        "using first",
                        p.doi, self._by_doi[p.doi], p.key,
                    )
                else:
                    self._by_doi[p.doi] = p.key
            if p.title:
                t = _normalize_title(p.title)
                if t:
                    # Same rule for title collisions.
                    if t not in self._by_title:
                        self._by_title[t] = p.key

    @classmethod
    def from_kb(cls, kb_root: Path) -> "LocalResolver":
        """Scan papers/*.md; build the resolver."""
        papers_dir = Path(kb_root) / "papers"
        results: list[ResolvedPaper] = []
        if not papers_dir.exists():
            return cls(results)

        for md in sorted(papers_dir.glob("*.md")):
            if md.name.startswith("."):
                continue
            try:
                post = frontmatter.load(str(md))
            except Exception as e:
                log.warning("skip %s: parse failed (%s)", md.name, e)
                continue
            fm = post.metadata
            key = md.stem
            doi = normalize_doi(fm.get("doi"))
            title = fm.get("title")
            results.append(ResolvedPaper(
                key=key, doi=doi, title=title, md_path=md,
            ))
        log.info("resolver: indexed %d local papers (%d with DOI)",
                 len(results), sum(1 for p in results if p.doi))
        return cls(results)

    # ------------------------------------------------------------

    def resolve_by_doi(self, doi: str | None) -> str | None:
        """Return local key if DOI is in KB, else None."""
        d = normalize_doi(doi)
        if not d:
            return None
        return self._by_doi.get(d)

    def resolve_by_title(self, title: str | None) -> str | None:
        """Fallback: find a paper whose title matches (after
        normalization). Only use when DOI is absent / unresolved.
        """
        if not title:
            return None
        t = _normalize_title(title)
        if not t:
            return None
        return self._by_title.get(t)

    def resolve(self, *, doi: str | None = None,
                title: str | None = None) -> str | None:
        """Combined lookup: DOI first, title as fallback."""
        return self.resolve_by_doi(doi) or self.resolve_by_title(title)

    # Iteration over all local papers — used by fetcher.
    def __iter__(self):
        return iter(self._papers)

    def __len__(self):
        return len(self._papers)

    @property
    def papers_with_doi(self) -> list[ResolvedPaper]:
        return [p for p in self._papers if p.doi]


def _normalize_title(title: str) -> str:
    """Canonical form for title-based matching.

    - Unicode NFKD (strip combining marks)
    - Lowercase
    - Replace non-alphanumeric with single space
    - Collapse whitespace
    - Strip
    """
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t
