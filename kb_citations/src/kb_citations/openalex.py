"""OpenAlex provider.

API docs: https://docs.openalex.org/

Endpoints:
  GET /works/doi:<doi>                     → work object with references
  GET /works?filter=cites:<work-id>        → who cites it (paginated)

OpenAlex differs from Semantic Scholar in a few ways we have to handle:

1. Work lookup by DOI returns the FULL work object, including a
   `referenced_works` array — just IDs, no titles. To get titles we
   have to do a second call batching IDs. We choose to fetch titles
   by batching (50 at a time) so rate-limit impact is minimal.

2. "Who cites X" uses the `/works` search endpoint with filter
   `cites:W123...`, which is paginated and returns full records.

Rate limits:
  - Unauthenticated: "polite" usage. Adding `?mailto=<email>` gets
    you into the polite pool (10 req/sec sustained).
  - Official API key: even higher (not needed for our scale).

Design: we ALWAYS use the mailto mechanism. Requires user to
configure an email (via env var or constructor arg) — we document
this and fail clearly if absent.
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

import httpx

from .provider import Reference, normalize_doi


log = logging.getLogger(__name__)

_BASE_URL = "https://api.openalex.org"

# Fields we need on returned work records. `referenced_works` gives
# us just the OpenAlex IDs; we follow up with a batch GET to hydrate
# titles + DOIs.
_WORK_SELECT = "id,doi,title,publication_year,authorships,referenced_works"


class OpenAlexProvider:
    """Fetches citation edges from OpenAlex.

    Args:
        mailto: your email (puts us in the polite pool). Required —
                unauth'd traffic can be throttled or blocked.
        hydrate_titles: whether to resolve referenced_works IDs to
                full Reference records (title, DOI, authors). True
                by default. Set False for speed when you only need
                DOIs — but most callers want titles.
    """
    name = "openalex"

    def __init__(
        self,
        mailto: str,
        *,
        hydrate_titles: bool = True,
        timeout: float = 30.0,
        request_interval: float = 0.12,   # ~8 req/sec
        max_retries: int = 3,
    ):
        if not mailto or "@" not in mailto:
            raise ValueError(
                "OpenAlex requires a contact email via mailto=... "
                "(for the polite pool). Pass --mailto or set "
                "OPENALEX_MAILTO env var."
            )
        self.mailto = mailto
        self.hydrate_titles = hydrate_titles
        self.timeout = timeout
        self.request_interval = request_interval
        self.max_retries = max_retries
        self._last_request_ts: float = 0.0

        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"User-Agent": f"kb-citations/0.1 ({mailto})"},
            timeout=timeout,
        )

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def get_references(self, doi: str, *, max_refs: int = 1000) -> Sequence[Reference]:
        """Return papers that `doi` cites."""
        doi = normalize_doi(doi)
        if not doi:
            return []
        work = self._get_json(f"/works/doi:{doi}",
                              params={"select": _WORK_SELECT})
        if not work:
            return []
        ref_ids = work.get("referenced_works") or []
        ref_ids = ref_ids[:max_refs]
        if not ref_ids:
            return []
        if not self.hydrate_titles:
            # Return skeletal Reference with only provider_id.
            return [
                Reference(
                    doi=None, title=None, year=None,
                    provider_id=_strip_openalex_prefix(rid),
                    provider="openalex",
                )
                for rid in ref_ids
            ]
        return self._hydrate_works(ref_ids)

    def get_citations(self, doi: str, *, max_cites: int = 200) -> Sequence[Reference]:
        """Return papers that cite `doi`."""
        doi = normalize_doi(doi)
        if not doi:
            return []
        # First resolve DOI → OpenAlex work ID (need the bare ID for
        # the `cites:` filter).
        work = self._get_json(f"/works/doi:{doi}", params={"select": "id"})
        if not work:
            return []
        work_id = _strip_openalex_prefix(work.get("id") or "")
        if not work_id:
            return []

        results: list[Reference] = []
        cursor = "*"
        page_size = 50

        while len(results) < max_cites and cursor:
            remaining = max_cites - len(results)
            per_page = min(page_size, remaining)
            payload = self._get_json(
                "/works",
                params={
                    "filter": f"cites:{work_id}",
                    "select": _WORK_SELECT,
                    "per-page": per_page,
                    "cursor": cursor,
                },
            )
            if not payload:
                break
            for raw in payload.get("results", []):
                ref = _to_reference(raw)
                if ref:
                    results.append(ref)
            cursor = (payload.get("meta") or {}).get("next_cursor")

        return results

    def get_paper_meta(self, doi: str) -> dict | None:
        """One GET for lightweight metadata: citation count + title + year.

        Used by `kb-citations refresh-counts` — cheap compared to a
        full referenced_works expansion. OpenAlex's `cited_by_count`
        is the incoming-citation count (equivalent to S2's
        citationCount).
        """
        doi = normalize_doi(doi)
        if not doi:
            return None
        work = self._get_json(
            f"/works/doi:{doi}",
            params={"select": "doi,title,publication_year,cited_by_count"},
        )
        if not work:
            return None
        return {
            "doi": doi,
            "citation_count": work.get("cited_by_count"),
            "title": work.get("title"),
            "year": work.get("publication_year"),
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

    def _hydrate_works(self, work_ids: list[str]) -> list[Reference]:
        """Given a list of OpenAlex work IDs/URLs, fetch their full
        records (title, DOI, authors) in batches of 50.

        Uses `filter=ids.openalex:W1|W2|...` to get 50 works in one
        request (OpenAlex's bulk lookup idiom)."""
        refs: list[Reference] = []
        # Strip any URL prefix on the IDs — API expects bare Wxxx.
        bare_ids = [_strip_openalex_prefix(x) for x in work_ids if x]
        bare_ids = [x for x in bare_ids if x]

        batch_size = 50
        for i in range(0, len(bare_ids), batch_size):
            batch = bare_ids[i:i + batch_size]
            filter_str = "ids.openalex:" + "|".join(batch)
            payload = self._get_json(
                "/works",
                params={
                    "filter": filter_str,
                    "select": _WORK_SELECT,
                    "per-page": len(batch),
                },
            )
            if not payload:
                continue
            for raw in payload.get("results", []):
                ref = _to_reference(raw)
                if ref:
                    refs.append(ref)
        return refs

    def _get_json(self, path: str, params: dict) -> dict | None:
        """GET with rate limiting + retry. Returns parsed JSON or
        None on unrecoverable failure."""
        # mailto goes on every request (polite pool).
        params = {**params, "mailto": self.mailto}

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
                    log.warning("OpenAlex %s: network error: %s", path, e)
                    return None
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 404:
                log.debug("OpenAlex %s: 404", path)
                return None
            if resp.status_code == 429:
                if attempt > self.max_retries:
                    return None
                # Honour Retry-After if present (see S2 code for the
                # same rationale). Capped at 120s so a misbehaving
                # upstream can't stall the whole batch.
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
                    sleep_for = min(120, sleep_for)
                log.info("OpenAlex %s: 429, sleeping %ds", path, sleep_for)
                time.sleep(sleep_for)
                continue
            if 500 <= resp.status_code < 600:
                if attempt > self.max_retries:
                    return None
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                log.warning("OpenAlex %s: %d: %s",
                            path, resp.status_code, resp.text[:200])
                return None
            try:
                return resp.json()
            except ValueError:
                return None


def _strip_openalex_prefix(wid: str) -> str:
    """OpenAlex IDs appear as 'https://openalex.org/W123...' in
    responses but the API accepts 'W123...' bare. Normalize to bare."""
    if not wid:
        return ""
    wid = wid.strip()
    for prefix in ("https://openalex.org/", "http://openalex.org/"):
        if wid.startswith(prefix):
            return wid[len(prefix):]
    return wid


def _to_reference(raw: dict) -> Reference | None:
    """Convert an OpenAlex work record into our Reference dataclass.

    OpenAlex shape (abbreviated):
        {
          "id": "https://openalex.org/W123",
          "doi": "https://doi.org/10.xx/yy",
          "title": "...",
          "publication_year": 2024,
          "authorships": [{"author": {"display_name": "Alice"}}, ...]
        }
    """
    doi = normalize_doi(raw.get("doi"))
    title = raw.get("title")
    if not doi and not title:
        return None
    year = raw.get("publication_year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    authors: list[str] = []
    for aship in raw.get("authorships") or []:
        author = aship.get("author") or {}
        name = author.get("display_name")
        if name:
            authors.append(name)
    return Reference(
        doi=doi,
        title=title,
        year=year,
        authors=authors,
        provider_id=_strip_openalex_prefix(raw.get("id") or ""),
        provider="openalex",
    )
