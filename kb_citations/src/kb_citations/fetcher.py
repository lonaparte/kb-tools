"""Batch fetcher: iterate all papers in a KB, call the provider,
cache results.

Flow:
    for paper in resolver:
        if cache is fresh: skip
        if paper has no DOI: skip (can't query by DOI)
        refs = provider.get_references(paper.doi)
        cites = provider.get_citations(paper.doi)  if ctx.fetch_citations
        cache.save(paper.key, refs, cites)

Prints progress so the user knows it's alive during the 20-min run.
Returns a FetchReport with counts.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable

from .cache import CitationCache
from .config import CitationsContext
from .provider import CitationProvider
from .resolver import LocalResolver


log = logging.getLogger(__name__)


@dataclass
class FetchReport:
    total_papers: int = 0
    skipped_no_doi: int = 0
    skipped_fresh_cache: int = 0
    fetched: int = 0
    fetch_errors: int = 0
    total_references_collected: int = 0
    total_citations_collected: int = 0


def fetch_all(
    ctx: CitationsContext,
    provider: CitationProvider,
    *,
    progress: Callable[[str], None] | None = None,
    max_api_calls: int | None = None,
    only_keys: list[str] | None = None,
) -> FetchReport:
    """Walk every paper in kb_root, fetch from provider, cache.

    Args:
        ctx, provider: as set up by CLI.
        progress: per-paper callback; defaults to stderr.
        max_api_calls: optional hard cap on total provider calls
            (not per-paper). Each paper uses 1 call without
            with-citations, 2 with. Cap protects against a flaky
            API key burning your quota on partial runs, or for
            sampling runs.
        only_keys: if given, restrict to just these Zotero keys
            (subset fetch). None = all papers with DOIs. Used by
            MCP `fetch_citations` tool when an agent wants to
            refresh just a handful of papers after importing.
    """
    if progress is None:
        progress = lambda s: print(s, file=sys.stderr, flush=True)

    resolver = LocalResolver.from_kb(ctx.kb_root)
    cache = CitationCache(ctx.kb_root)
    cache.ensure_dirs()

    # Stats bookkeeping: in subset mode, `total_papers` and
    # `skipped_no_doi` must be computed against the REQUESTED subset,
    # not the whole library — otherwise the user asking
    # `--only-key K1,K2` sees "skipped_no_doi=1134" because 1154 full
    # library minus 20 with-DOI subset papers looks like 1134
    # "skipped for no DOI". The actual question "how many of my
    # requested keys lack DOI?" gets lost. Previously this block used
    # `len(resolver)` unconditionally which produced exactly that
    # misleading output in subset runs.
    with_doi = resolver.papers_with_doi
    if only_keys is not None:
        wanted = set(only_keys)
        requested_total = len(wanted)
        with_doi = [p for p in with_doi if p.key in wanted]
        report = FetchReport(total_papers=requested_total)
        progress(
            f"kb-citations fetch: subset of {requested_total} requested, "
            f"{len(with_doi)} found with DOI, provider={provider.name}"
        )
    else:
        report = FetchReport(total_papers=len(resolver))
        progress(
            f"kb-citations fetch: {report.total_papers} papers, "
            f"{len(with_doi)} with DOI, provider={provider.name}"
        )
    if max_api_calls is not None:
        progress(f"  max-api-calls cap: {max_api_calls}")

    calls_used = 0
    cap_hit = False
    calls_per_paper = 2 if ctx.fetch_citations else 1

    for i, paper in enumerate(with_doi, start=1):
        prefix = f"[{i}/{len(with_doi)}] {paper.key}"

        if ctx.freshness_days is not None and cache.is_fresh(
            paper.key, max_age_days=ctx.freshness_days,
        ):
            report.skipped_fresh_cache += 1
            progress(f"{prefix}: cached (skip)")
            continue

        # Budget check: would this paper exceed the cap?
        if (max_api_calls is not None
                and calls_used + calls_per_paper > max_api_calls):
            if not cap_hit:
                progress(
                    f"{prefix}: hit --max-api-calls={max_api_calls} "
                    f"(used {calls_used}); stopping fetch loop"
                )
                cap_hit = True
            break

        try:
            refs = list(provider.get_references(
                paper.doi, max_refs=ctx.max_refs,
            ))
            calls_used += 1
            cites: list = []
            if ctx.fetch_citations:
                cites = list(provider.get_citations(
                    paper.doi, max_cites=ctx.max_cites,
                ))
                calls_used += 1
        except Exception as e:
            report.fetch_errors += 1
            progress(f"{prefix}: ERROR {e!r}")
            continue

        cache.save(
            paper.key, provider=provider.name,
            references=refs, citations=cites, doi=paper.doi,
        )
        report.fetched += 1
        report.total_references_collected += len(refs)
        report.total_citations_collected += len(cites)
        progress(
            f"{prefix}: refs={len(refs)} cites={len(cites)}"
        )

    report.skipped_no_doi = report.total_papers - len(with_doi)
    progress(
        f"done: fetched={report.fetched}, "
        f"skipped_fresh={report.skipped_fresh_cache}, "
        f"skipped_no_doi={report.skipped_no_doi}, "
        f"errors={report.fetch_errors}, "
        f"refs_collected={report.total_references_collected}, "
        f"cites_collected={report.total_citations_collected}"
    )
    return report


def build_provider(ctx: CitationsContext) -> CitationProvider:
    """Factory: choose provider implementation based on ctx."""
    if ctx.provider == "semantic_scholar":
        from .semantic_scholar import SemanticScholarProvider
        api_key = ctx.api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        return SemanticScholarProvider(api_key=api_key)
    if ctx.provider == "openalex":
        from .openalex import OpenAlexProvider
        mailto = ctx.mailto or os.environ.get("OPENALEX_MAILTO")
        if not mailto:
            raise ValueError(
                "OpenAlex requires a contact email: pass --mailto "
                "or set OPENALEX_MAILTO env var."
            )
        return OpenAlexProvider(mailto=mailto)
    raise ValueError(
        f"unknown provider {ctx.provider!r}; "
        "expected 'semantic_scholar' or 'openalex'"
    )
