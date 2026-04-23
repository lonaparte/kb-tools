"""Semantic Scholar provider.

API docs: https://api.semanticscholar.org/api-docs/graph

Endpoints used:
  GET /graph/v1/paper/DOI:<doi>/references
  GET /graph/v1/paper/DOI:<doi>/citations

Rate limit:
  - Anonymous: 100 req / 5 min (rough; can spike)
  - With API key (free, sign up): 1 req/sec sustained

Pagination: both endpoints are paginated (offset/limit, max 1000).
For references we default to 1000 — most papers cite < 500. For
citations we cap at 200 by default (usually we don't need them all).

Failure modes we handle:
  - 404 (paper not in S2) — return empty list
  - 429 (rate limit) — exponential backoff, up to 3 retries
  - 5xx — retry once
  - Network error — retry once
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

import httpx

from .provider import Reference, normalize_doi


log = logging.getLogger(__name__)

_BASE_URL = "https://api.semanticscholar.org/graph/v1"

# Fields we want on each reference/citation record.
# externalIds gives us DOI; paperId is S2's internal ID.
_REF_FIELDS = "externalIds,title,year,authors.name"


class SemanticScholarProvider:
    """Fetches citation edges from Semantic Scholar.

    Pass `api_key` (from https://www.semanticscholar.org/product/api)
    to get higher rate limits. Without it, still works for small jobs
    (< 100 req / 5 min).
    """
    name = "semantic_scholar"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        request_interval: float = 1.1,   # seconds between requests
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.timeout = timeout
        # S2 recommends 1 req/sec with API key; be slightly over to
        # avoid tripping the limit on bursts.
        self.request_interval = request_interval
        self.max_retries = max_retries
        self._last_request_ts: float = 0.0

        headers = {"User-Agent": "kb-citations/0.1"}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = httpx.Client(
            base_url=_BASE_URL, headers=headers, timeout=timeout,
        )

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def get_references(self, doi: str, *, max_refs: int = 1000) -> Sequence[Reference]:
        """Return the papers that `doi` cites."""
        doi = normalize_doi(doi)
        if not doi:
            return []
        return self._paginate(
            path=f"/paper/DOI:{doi}/references",
            wrap_key="citedPaper",     # S2's wrapper key for references
            max_total=max_refs,
        )

    def get_citations(self, doi: str, *, max_cites: int = 200) -> Sequence[Reference]:
        """Return the papers that cite `doi`."""
        doi = normalize_doi(doi)
        if not doi:
            return []
        return self._paginate(
            path=f"/paper/DOI:{doi}/citations",
            wrap_key="citingPaper",
            max_total=max_cites,
        )

    def get_paper_meta(self, doi: str) -> dict | None:
        """One GET for a single paper's top-level metadata.

        Used by `kb-citations refresh-counts` — we only want
        citationCount, title, year. No pagination, one request per
        paper; O(N) instead of the O(N * avg_refs) cost of a full
        reference fetch.
        """
        doi = normalize_doi(doi)
        if not doi:
            return None
        payload = self._get_json(
            path=f"/paper/DOI:{doi}",
            params={"fields": "citationCount,title,year,externalIds"},
        )
        if payload is None:
            return None
        return {
            "doi": doi,
            "citation_count": payload.get("citationCount"),
            "title": payload.get("title"),
            "year": payload.get("year"),
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _paginate(self, path: str, wrap_key: str, max_total: int) -> list[Reference]:
        """Walk paginated endpoint; stop at max_total or natural end."""
        results: list[Reference] = []
        offset = 0
        page_size = 100   # S2 allows up to 1000 but smaller keeps each request fast

        while len(results) < max_total:
            remaining = max_total - len(results)
            limit = min(page_size, remaining)
            params = {
                "offset": offset,
                "limit": limit,
                "fields": _REF_FIELDS,
            }
            payload = self._get_json(path, params=params)
            if payload is None:
                break
            data = payload.get("data") or []
            if not data:
                break

            for entry in data:
                raw = entry.get(wrap_key)
                if not raw:
                    continue
                ref = _to_reference(raw)
                if ref:
                    results.append(ref)

            # S2 returns `next` in the payload only if more pages.
            next_offset = payload.get("next")
            if next_offset is None:
                break
            offset = next_offset

        return results

    def _get_json(self, path: str, params: dict) -> dict | None:
        """Perform GET with rate limiting + retry. Returns JSON dict
        or None on unrecoverable failure."""
        # Rate limit: sleep until the interval since last request
        # has elapsed.
        now = time.monotonic()
        wait = self.request_interval - (now - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.get(path, params=params)
                self._last_request_ts = time.monotonic()
            except httpx.HTTPError as e:
                if attempt > self.max_retries:
                    log.warning("S2 %s: network error after retries: %s", path, e)
                    return None
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 404:
                log.debug("S2 %s: 404 (not in corpus)", path)
                return None
            if resp.status_code in (401, 403):
                # 401/403 almost always mean the API key is invalid,
                # expired, revoked, or has hit a per-key daily quota.
                # Print a clear suggestion ONCE and bail.
                if not getattr(self, "_warned_auth", False):
                    self._warned_auth = True
                    import sys as _sys
                    print(
                        f"\nkb-citations: Semantic Scholar returned "
                        f"{resp.status_code} {resp.reason_phrase}.\n"
                        "  Your SEMANTIC_SCHOLAR_API_KEY may be invalid, "
                        "expired, or out of quota.\n"
                        "  Try:  kb-citations --provider openalex fetch "
                        "--mailto you@example.com\n"
                        "  (set OPENALEX_MAILTO in ~/.bashrc to avoid "
                        "--mailto each time)\n",
                        file=_sys.stderr,
                    )
                return None
            if resp.status_code == 429:
                if attempt > self.max_retries:
                    log.warning("S2 %s: rate-limited after retries", path)
                    return None
                # Respect the server's Retry-After header if present
                # (RFC 7231 §7.1.3). Can be a delta-seconds integer or
                # an HTTP-date; we parse just the integer form since
                # that's what S2 sends. Fall back to exponential if
                # the header is missing or malformed, capping at 60s
                # so a badly-phrased 429 can't stall the CLI for
                # hours.
                sleep_for = None
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        sleep_for = max(0, int(ra.strip()))
                    except ValueError:
                        sleep_for = None
                if sleep_for is None:
                    sleep_for = min(60, 2 ** attempt * 5)
                else:
                    # Hard cap even when the server asks for more —
                    # a 10-minute wait in a 100-paper batch is worse
                    # than aborting and retrying later.
                    sleep_for = min(120, sleep_for)
                log.info("S2 %s: 429, sleeping %ds", path, sleep_for)
                time.sleep(sleep_for)
                continue
            if 500 <= resp.status_code < 600:
                if attempt > self.max_retries:
                    log.warning("S2 %s: %d after retries", path, resp.status_code)
                    return None
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                # Defensive: any other non-200 (shouldn't happen with
                # this API). If it's 4xx that looks like auth, still
                # suggest provider switch.
                hint = ""
                if 400 <= resp.status_code < 500:
                    hint = (" (4xx errors often mean key problems; "
                            "try --provider openalex)")
                log.warning("S2 %s: unexpected %d%s: %s",
                            path, resp.status_code, hint, resp.text[:200])
                return None

            try:
                return resp.json()
            except ValueError:
                log.warning("S2 %s: non-JSON response", path)
                return None


def _to_reference(raw: dict) -> Reference | None:
    """Convert a S2 paper record into our Reference dataclass.

    S2 shape (abbreviated):
        {
          "paperId": "abc123...",
          "externalIds": {"DOI": "10.xx/yy", "ArXiv": "..."},
          "title": "...",
          "year": 2024,
          "authors": [{"name": "Alice"}, ...]
        }
    Missing fields are common and fine.
    """
    ext = raw.get("externalIds") or {}
    doi = normalize_doi(ext.get("DOI"))
    title = raw.get("title")
    if not doi and not title:
        # Useless record — skip entirely.
        return None
    authors = [
        a.get("name", "") for a in (raw.get("authors") or [])
        if a.get("name")
    ]
    year = raw.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    return Reference(
        doi=doi,
        title=title,
        year=year,
        authors=authors,
        provider_id=raw.get("paperId"),
        provider="semantic_scholar",
    )
