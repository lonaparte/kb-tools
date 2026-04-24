"""Regression for the v0.28.2 BadRequestError typed classification.

Reviewer finding (pre-0.28.2):
  - A BadRequestError class exists in summarize.py with code='bad_request'.
  - But the Gemini provider raises GENERIC SummarizerError for HTTP
    400 / 404, not BadRequestError.
  - import_fulltext.py string-matches "400" in the error message to
    decide log category, and does NOT change retry behavior based on
    the classification. Result: a permanently-broken model name
    produces N failed papers (one SummarizerError each) instead of
    one "bad model, giving up / switching fallback" event.

v0.28.2 fixes both:
  1. GeminiProvider.complete() raises BadRequestError specifically
     for HTTP 400 and 404. (Test: stub urlopen to return 400/404
     and inspect the raised exception class.)
  2. import_fulltext's retry loop now has an except-branch for
     BadRequestError that calls _try_fallback_after_bad_request.
     For "model not found" shapes, it switches to fallback; for
     per-paper bad requests, it abandons the paper but continues
     the batch.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


class _FakeResp:
    def __init__(self, body_bytes):
        self._body = body_bytes
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self):
        return self._body


def _raise_http(code: int, detail: dict):
    """Builder for urllib.error.HTTPError with a JSON detail body."""
    from urllib.error import HTTPError
    url = "https://example/"
    err = HTTPError(url, code, "err", {}, None)
    err.read = lambda: json.dumps(detail).encode()
    raise err


def test_gemini_400_raises_bad_request_error():
    from kb_importer.summarize import GeminiProvider, BadRequestError

    def fake_urlopen(req, timeout=120):
        _raise_http(400, {"error": {"code": 400,
                                     "message": "Invalid request"}})

    prov = GeminiProvider(api_key="fake", model="gemini-3.1-pro-preview")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(BadRequestError) as exc:
            prov.complete("sys", "user")
    assert "400" in str(exc.value)
    # Also confirm the typed code:
    assert exc.value.code == "bad_request"


def test_gemini_404_raises_bad_request_error():
    """404 'model not found' is also permanent; must surface as
    BadRequestError so the importer can swap to fallback."""
    from kb_importer.summarize import GeminiProvider, BadRequestError

    def fake_urlopen(req, timeout=120):
        _raise_http(404, {"error": {
            "code": 404,
            "message": "models/gemini-nonexistent is not found",
        }})

    prov = GeminiProvider(api_key="fake", model="gemini-nonexistent")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(BadRequestError) as exc:
            prov.complete("sys", "user")
    assert "not found" in str(exc.value).lower()


def test_gemini_429_still_raises_quota_not_bad_request():
    """Regression guard: we didn't accidentally route quota errors
    into the BadRequest path."""
    from kb_importer.summarize import (
        GeminiProvider, BadRequestError, QuotaExhaustedError,
    )

    def fake_urlopen(req, timeout=120):
        _raise_http(429, {"error": {
            "code": 429,
            "message": "RESOURCE_EXHAUSTED: generate_content_free_tier_requests",
            "details": [{"quotaId": "generate_content_free_tier_requests_per_day"}],
        }})

    prov = GeminiProvider(api_key="fake", model="gemini-3.1-pro-preview")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(QuotaExhaustedError):
            prov.complete("sys", "user")


def test_gemini_500_generic_summarizer_error():
    """5xx is still a generic SummarizerError — retryable infra
    failure, not a permanent bad-request."""
    from kb_importer.summarize import (
        GeminiProvider, BadRequestError, SummarizerError,
    )

    def fake_urlopen(req, timeout=120):
        _raise_http(500, {"error": {"code": 500, "message": "internal"}})

    prov = GeminiProvider(api_key="fake", model="gemini-3.1-pro-preview")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(SummarizerError) as exc:
            prov.complete("sys", "user")
    assert not isinstance(exc.value, BadRequestError)
