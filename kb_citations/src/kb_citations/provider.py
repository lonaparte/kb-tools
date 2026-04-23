"""Abstract citation provider interface.

Each provider (Semantic Scholar, OpenAlex, ...) implements the same
protocol so the fetcher can swap between them. Two ops:

- `get_references(doi)` — who does this paper cite
- `get_citations(doi)` — who cites this paper

Both return a list of `Reference` — normalized across providers, so
downstream code (linker, resolver) doesn't care which API produced
the data.

Why a Protocol (not ABC)? Duck typing is enough here; the two
implementations are small and don't share code. Protocol also means
tests can mock with a plain class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Reference:
    """One edge of the citation graph, in provider-neutral form.

    `doi` is the primary key for resolution into your KB. Everything
    else is for when the DOI is missing or unresolvable (fallback
    match on title). `provider` is stamped so audit / debugging can
    trace which API produced this edge.
    """
    doi: str | None                 # lowercased; no URL prefix
    title: str | None               # may be None for sparse records
    year: int | None
    authors: list[str] = field(default_factory=list)
    # Identifier as returned by the provider (S2 paperId, OpenAlex
    # work ID). Useful for re-querying the provider without re-
    # discovering the paper.
    provider_id: str | None = None
    provider: str = ""              # "semantic_scholar" | "openalex"
    # Where this reference appears in the source paper (intro /
    # methods / ...). Most providers don't give this; field reserved
    # for GROBID-style PDF-level extraction later.
    context: str | None = None


class CitationProvider(Protocol):
    """Every provider exposes the same three methods.

    Keyword args `max_refs` / `max_cites` are part of the contract
    (fetcher always passes them from CitationsContext.max_refs /
    max_cites). A concrete provider may use a larger default but MUST
    accept the kwarg, otherwise `fetcher.fetch_all` crashes with
    "unexpected keyword argument". Previously the Protocol only
    declared `(self, doi: str)` — both shipped implementations
    happened to accept the kwargs so it didn't blow up, but a new
    Protocol-conformant provider would have been broken at runtime.
    """

    name: str  # "semantic_scholar" | "openalex"

    def get_references(
        self, doi: str, *, max_refs: int = 1000,
    ) -> Sequence[Reference]:
        """Return the list of papers that `doi` cites (outgoing).

        `max_refs` caps how many references the provider should
        return. Providers with paginated APIs should stop fetching
        once they've collected this many; providers that return all
        references in one call may truncate client-side. A typical
        paper has 30-50 refs so the default 1000 is a soft ceiling.
        """
        ...

    def get_citations(
        self, doi: str, *, max_cites: int = 200,
    ) -> Sequence[Reference]:
        """Return the list of papers that cite `doi` (incoming).

        May be slow / paginated on heavily-cited papers. Providers
        are free to truncate — we don't need the full 2000-citation
        list to get value. `max_cites` gives a soft ceiling.
        """
        ...

    def get_paper_meta(self, doi: str) -> dict | None:
        """Return lightweight metadata for a single paper:

            {
              "doi": "10.1109/...",
              "citation_count": 42,   # 0 is valid; None = unknown
              "title": "...",          # may be None
              "year": 2023,            # may be None
            }

        or None if the DOI isn't in the provider's corpus. One HTTP
        call per paper — cheaper than get_references / get_citations.
        Used by `kb-citations refresh-counts` to bulk-update just the
        citation_count column without re-fetching full reference
        lists.
        """
        ...


def normalize_doi(doi: str | None) -> str | None:
    """Standard form: lowercase, no URL prefix, stripped.

    Accepts: '10.1109/...', 'doi:10.1109/...', 'https://doi.org/10.1109/...'
    Returns: '10.1109/...' or None if empty.
    """
    if not doi:
        return None
    d = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/",
                   "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.strip("/ \t")
    return d or None
