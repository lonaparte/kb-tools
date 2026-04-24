"""1.4.0: quota classification for non-Gemini providers.

Pre-1.4, OpenAI / DeepSeek / OpenRouter 429 landed in the generic
SummarizerError path. These tests verify the 429 → QuotaExhaustedError
routing + retry_after extraction works across provider-shape error
bodies.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from kb_importer import summarize as S


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(S, "_retry_sleep", lambda attempt: None)


class _FakeHTTPError(urllib.error.HTTPError):
    """HTTPError with a mock `headers` object supporting `.get()`."""
    def __init__(self, code, body, retry_after_header=None):
        headers_dict = {}
        if retry_after_header is not None:
            headers_dict["Retry-After"] = retry_after_header

        class _H:
            def get(self, key, default=None):
                return headers_dict.get(key, default)

        super().__init__(
            url="x", code=code, msg="err", hdrs=None,
            fp=io.BytesIO(body.encode()),
        )
        self.headers = _H()


def _make_urlopen(script):
    state = {"calls": 0}
    def _urlopen(req, timeout=120):
        i = state["calls"]
        state["calls"] += 1
        outcome = script[min(i, len(script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        raise RuntimeError("test shouldn't reach happy path")
    _urlopen._state = state
    return _urlopen


# ----------------------------------------------------------------------
# Classification helpers
# ----------------------------------------------------------------------


def test_classify_openai_insufficient_quota_as_daily():
    s = 'error.type is insufficient_quota'
    assert S._classify_quota_kind(s) == "daily"


def test_classify_openai_rate_limit_as_rate():
    s = 'error.type is rate_limit_exceeded'
    assert S._classify_quota_kind(s) == "rate"


def test_classify_gemini_per_day_as_daily():
    assert S._classify_quota_kind("per_day quota exceeded") == "daily"


def test_classify_gemini_per_minute_as_rate():
    assert S._classify_quota_kind("per_minute limit reached") == "rate"


def test_classify_unknown_returns_unknown():
    assert S._classify_quota_kind("something else happened") == "unknown"


# ----------------------------------------------------------------------
# retry_after extraction
# ----------------------------------------------------------------------


def test_retry_after_prefers_http_header():
    err = _FakeHTTPError(429, "body text", retry_after_header="42")
    assert S._extract_retry_after(err, "body text") == 42.0


def test_retry_after_falls_back_to_body_gemini_shape():
    err = _FakeHTTPError(429, "Please retry in 12.5s")
    assert S._extract_retry_after(err, "Please retry in 12.5s") == 12.5


def test_retry_after_falls_back_to_openai_shape_ms():
    body = "Please try again in 200ms."
    err = _FakeHTTPError(429, body)
    value = S._extract_retry_after(err, body)
    assert value is not None
    assert abs(value - 0.2) < 1e-6


def test_retry_after_falls_back_to_openai_shape_seconds():
    body = "Please try again in 30s."
    err = _FakeHTTPError(429, body)
    assert S._extract_retry_after(err, body) == 30.0


# ----------------------------------------------------------------------
# End-to-end: OpenAIChatProvider 429 → QuotaExhaustedError
# ----------------------------------------------------------------------


def test_openai_429_raises_quota_exhausted(monkeypatch):
    """Pre-1.4 this raised SummarizerError; now it's QuotaExhaustedError
    with classified type + retry_after so the batch fallback_state
    can react the same way it does for Gemini."""
    err = _FakeHTTPError(
        429,
        'error.type is rate_limit_exceeded: Please try again in 20s.',
        retry_after_header="20",
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(S.QuotaExhaustedError) as exc:
        prov.complete("sys", "user")
    e = exc.value
    assert e.quota_type == "rate"
    assert e.retry_after == 20.0
    assert e.model == "gpt-4o-mini"
    # No retry — quota-error short-circuits.
    assert urlopen._state["calls"] == 1


def test_openai_402_insufficient_quota_raises_quota_daily(monkeypatch):
    """OpenAI sends 402 (payment required) + type=insufficient_quota
    when the billing account is out. Route through the same quota
    path so fallback_state can decide."""
    err = _FakeHTTPError(
        402,
        'error.type is insufficient_quota: You exceeded your '
        'current quota, please check your plan and billing details.',
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="gpt-4o-mini")
    with pytest.raises(S.QuotaExhaustedError) as exc:
        prov.complete("sys", "user")
    assert exc.value.quota_type == "daily"


def test_openai_404_still_bad_request(monkeypatch):
    """404 = model not found. 1.4.0 added explicit 404 handling so it
    doesn't get routed through the 5xx retry path."""
    err = _FakeHTTPError(404, "model gpt-99 not found")
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="gpt-99")
    with pytest.raises(S.BadRequestError):
        prov.complete("sys", "user")
    # No retry.
    assert urlopen._state["calls"] == 1
