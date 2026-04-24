"""Transient-transport retry behavior in the stdlib-urllib HTTP
paths (OpenAIChatProvider + GeminiProvider).

We monkeypatch `urllib.request.urlopen` to return a sequence of
scripted responses / exceptions, then verify the provider's
`complete()`:
  - retries URLError / TimeoutError / HTTP 5xx up to _DEFAULT_RETRIES
  - does NOT retry HTTP 400 / HTTP 404 / BadRequestError causes
  - gives up after exhausting retry budget
  - doesn't sleep between retries when we monkeypatch _retry_sleep
    (keeps the test suite fast)
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from kb_importer import summarize as S


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """Replace the backoff sleep with a no-op so these tests run
    instantly. Production code still sleeps."""
    monkeypatch.setattr(S, "_retry_sleep", lambda attempt: None)


def _good_response_bytes() -> bytes:
    """Minimal OpenAI-shaped chat response."""
    return json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }).encode("utf-8")


def _good_gemini_response_bytes() -> bytes:
    return json.dumps({
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }).encode("utf-8")


class _FakeResponse:
    """Minimal `with urlopen(...) as resp` contract."""
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._stream.read()


def _make_urlopen(script):
    """Given a list of outcomes (exceptions to raise or bytes to
    return), produce a stateful `urlopen` substitute."""
    state = {"calls": 0}
    def _urlopen(req, timeout=120):
        i = state["calls"]
        state["calls"] += 1
        outcome = script[min(i, len(script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)
    _urlopen._state = state
    return _urlopen


# ---------------------------------------------------------------------
# OpenAIChatProvider
# ---------------------------------------------------------------------


def test_chat_succeeds_first_try(monkeypatch):
    """Baseline: a happy-path response on the first attempt."""
    urlopen = _make_urlopen([_good_response_bytes()])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    text, pin, pout = prov.complete("sys", "user")
    assert text == "ok"
    assert urlopen._state["calls"] == 1


def test_chat_retries_urlerror_then_succeeds(monkeypatch):
    urlopen = _make_urlopen([
        urllib.error.URLError("conn reset"),
        urllib.error.URLError("dns timeout"),
        _good_response_bytes(),
    ])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    text, _, _ = prov.complete("sys", "user")
    assert text == "ok"
    # 1 initial + 2 retries = 3 calls
    assert urlopen._state["calls"] == 3


def test_chat_gives_up_after_retry_budget(monkeypatch):
    """All three attempts fail with URLError → SummarizerError."""
    urlopen = _make_urlopen([urllib.error.URLError("boom")] * 5)
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    with pytest.raises(S.SummarizerError) as exc:
        prov.complete("sys", "user")
    assert "network error" in str(exc.value)
    assert urlopen._state["calls"] == S._DEFAULT_RETRIES + 1


def test_chat_retries_5xx(monkeypatch):
    err500 = urllib.error.HTTPError(
        url="x", code=503, msg="unavailable", hdrs=None,
        fp=io.BytesIO(b"overload"),
    )
    urlopen = _make_urlopen([err500, _good_response_bytes()])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    text, _, _ = prov.complete("sys", "user")
    assert text == "ok"
    assert urlopen._state["calls"] == 2


def test_chat_does_not_retry_400(monkeypatch):
    """HTTP 400: deterministic client error, never retry."""
    err400 = urllib.error.HTTPError(
        url="x", code=400, msg="bad request", hdrs=None,
        fp=io.BytesIO(b"bad prompt"),
    )
    urlopen = _make_urlopen([err400])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    with pytest.raises(S.BadRequestError):
        prov.complete("sys", "user")
    # Exactly one attempt — no retries on 400.
    assert urlopen._state["calls"] == 1


def test_chat_does_not_retry_timeout_error_consumes_budget(monkeypatch):
    """TimeoutError (builtin, raised by urlopen on socket timeout)
    counts as transient; retried like URLError."""
    urlopen = _make_urlopen([
        TimeoutError("socket timed out"),
        _good_response_bytes(),
    ])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    text, _, _ = prov.complete("sys", "user")
    assert text == "ok"
    assert urlopen._state["calls"] == 2


# ---------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------


def test_gemini_retries_urlerror(monkeypatch):
    urlopen = _make_urlopen([
        urllib.error.URLError("timeout"),
        _good_gemini_response_bytes(),
    ])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.GeminiProvider(api_key="k", model="gemini-2.5-flash")
    text, _, _ = prov.complete("sys", "user")
    assert text == "ok"
    assert urlopen._state["calls"] == 2


def test_gemini_429_bails_out_as_quota(monkeypatch):
    """429 goes through QuotaExhaustedError path, not the retry
    loop. The caller's fallback_state handles quota specifically."""
    err429 = urllib.error.HTTPError(
        url="x", code=429, msg="rate limited", hdrs=None,
        fp=io.BytesIO(b'{"error":{"message":"Quota per_day exceeded"}}'),
    )
    urlopen = _make_urlopen([err429])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.GeminiProvider(api_key="k", model="gemini-2.5-flash")
    with pytest.raises(S.QuotaExhaustedError):
        prov.complete("sys", "user")
    assert urlopen._state["calls"] == 1  # no retry


def test_gemini_400_permanent(monkeypatch):
    err400 = urllib.error.HTTPError(
        url="x", code=400, msg="bad", hdrs=None,
        fp=io.BytesIO(b"bad"),
    )
    urlopen = _make_urlopen([err400])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.GeminiProvider(api_key="k", model="gemini-2.5-flash")
    with pytest.raises(S.BadRequestError):
        prov.complete("sys", "user")
    assert urlopen._state["calls"] == 1
