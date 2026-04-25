"""Provider mock coverage for failure modes 1.4.0 didn't already test.

Locks in OpenAIChatProvider + GeminiProvider behavior under:
  - 401 / 403 (auth / forbidden) — should bubble as SummarizerError
    without retry. Distinct from 400 BadRequestError (which is the
    "your request was malformed" semantic) — auth issues aren't
    going to fix themselves on retry, but they're also not signal
    to switch model.
  - 200 with malformed JSON body.
  - 200 with valid JSON but missing the choices/candidates path
    (provider returned an error wrapped in a 200, common with
    misbehaving gateways).
  - 429 body shape variants: Gemini RESOURCE_EXHAUSTED, OpenRouter
    upstream-wrapped, DeepSeek shape.
  - URLError with timeout-shaped reason vs DNS-shaped reason —
    both should retry but the message should be informative.
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from kb_importer import summarize as S


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(S, "_retry_sleep", lambda attempt: None)


def _make_urlopen(script):
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


class _FakeResponse:
    def __init__(self, body):
        self._s = io.BytesIO(body)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._s.read()


def _hdr_dict(d):
    """Build a headers-like object .get()-able by HTTPError."""
    class _H:
        def get(self, k, default=None):
            return d.get(k, default)
    return _H()


# ----------------------------------------------------------------------
# Auth errors — 401 / 403
# ----------------------------------------------------------------------


def test_chat_401_unauthorized_no_retry(monkeypatch):
    """401 = bad / revoked API key. Retrying with the same key is
    pointless; surface as SummarizerError without burning the retry
    budget. Specifically NOT BadRequestError (input is fine; auth is
    what's broken)."""
    err = urllib.error.HTTPError(
        url="x", code=401, msg="unauthorized", hdrs=None,
        fp=io.BytesIO(b'{"error":{"message":"Invalid API key"}}'),
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="bad-key", model="m")
    with pytest.raises(S.SummarizerError) as exc:
        prov.complete("sys", "user")
    assert "401" in str(exc.value)
    # 1 attempt — no retry.
    assert urlopen._state["calls"] == 1
    # Specifically NOT BadRequestError. (BadRequestError is for
    # malformed prompts, not auth issues.)
    assert not isinstance(exc.value, S.BadRequestError)


def test_chat_403_forbidden_no_retry(monkeypatch):
    """403 = key valid but lacks permission for this model /
    endpoint. Same disposition as 401."""
    err = urllib.error.HTTPError(
        url="x", code=403, msg="forbidden", hdrs=None,
        fp=io.BytesIO(b'{"error":{"message":"Access to model ABC denied"}}'),
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    with pytest.raises(S.SummarizerError):
        prov.complete("sys", "user")
    assert urlopen._state["calls"] == 1


# ----------------------------------------------------------------------
# Malformed 200 responses
# ----------------------------------------------------------------------


def test_chat_200_invalid_json_raises_bad_request(monkeypatch):
    """A 200 with non-JSON body is a contract violation — usually a
    proxy returning HTML on overload. Route through BadRequestError
    so the events-log classifier puts it in `llm_bad_request`,
    distinguishing it from network errors."""
    bad_body = b"<html><body>503 from upstream proxy</body></html>"
    urlopen = _make_urlopen([bad_body])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    with pytest.raises(S.BadRequestError) as exc:
        prov.complete("sys", "user")
    assert "non-JSON" in str(exc.value)


def test_chat_200_missing_choices_raises_summarizer_error(monkeypatch):
    """Some misbehaving gateways return 200 + {"error":{...}} instead
    of an HTTP error code. The chat provider needs `choices[0].message
    .content`; missing → SummarizerError with a useful message."""
    body = json.dumps({"error": {"message": "actually failed"}}).encode()
    urlopen = _make_urlopen([body])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    with pytest.raises(S.SummarizerError) as exc:
        prov.complete("sys", "user")
    msg = str(exc.value)
    assert "choices" in msg


# ----------------------------------------------------------------------
# 429 body-shape variants
# ----------------------------------------------------------------------


def test_chat_429_openrouter_upstream_wrapped(monkeypatch):
    """OpenRouter wraps the upstream provider's error message in
    its own envelope. Make sure we still classify the quota_type
    correctly through the wrap."""
    # Realistic OpenRouter 429 shape (their docs show this format).
    body = json.dumps({
        "error": {
            "message": "Rate limit exceeded: free-models-per-day",
            "code": 429,
            "metadata": {
                "raw": "Provider returned 429 (rate_limit_exceeded)",
                "provider_name": "OpenAI",
            },
        }
    }).encode()
    err = urllib.error.HTTPError(
        url="x", code=429, msg="rl", hdrs=None,
        fp=io.BytesIO(body),
    )
    err.headers = _hdr_dict({"Retry-After": "60"})
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(
        api_key="k", model="x/y", name="openrouter",
    )
    with pytest.raises(S.QuotaExhaustedError) as exc:
        prov.complete("sys", "user")
    e = exc.value
    # "rate_limit_exceeded" + "per-day" both visible in body; the
    # daily token is more specific so we expect that to win.
    # _classify_quota_kind picks insufficient_quota / rate_limit
    # before per_day; for this body, "rate_limit_exceeded" matches
    # the rate_limit branch first → "rate".
    assert e.quota_type in ("rate", "daily")
    # Retry-After header should be honored.
    assert e.retry_after == 60.0


def test_chat_429_no_retry_after_no_body(monkeypatch):
    """Worst-case: 429 with no header AND no informative body. We
    still produce a QuotaExhaustedError with retry_after=None and
    the caller's quota-handling can fall back to its default sleep."""
    err = urllib.error.HTTPError(
        url="x", code=429, msg="rate", hdrs=None,
        fp=io.BytesIO(b""),
    )
    err.headers = _hdr_dict({})
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    with pytest.raises(S.QuotaExhaustedError) as exc:
        prov.complete("sys", "user")
    e = exc.value
    assert e.retry_after is None
    assert e.quota_type == "unknown"


def test_gemini_resource_exhausted_path(monkeypatch):
    """Gemini sometimes returns 429 with body containing
    `RESOURCE_EXHAUSTED` and no per_day/per_minute hint. Should
    still classify and raise QuotaExhaustedError (the existing
    Gemini branch already handles `RESOURCE_EXHAUSTED in detail`)."""
    body = json.dumps({
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded.",
        }
    }).encode()
    err = urllib.error.HTTPError(
        url="x", code=429, msg="resource", hdrs=None,
        fp=io.BytesIO(body),
    )
    err.headers = _hdr_dict({})
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.GeminiProvider(api_key="k", model="gemini-2.5-flash")
    with pytest.raises(S.QuotaExhaustedError) as exc:
        prov.complete("sys", "user")
    e = exc.value
    # `RESOURCE_EXHAUSTED` alone doesn't say daily vs rate → unknown.
    assert e.quota_type == "unknown"


def test_chat_deepseek_shape_400_for_unsupported_model(monkeypatch):
    """DeepSeek and other OpenAI-compatible gateways sometimes return
    400 with a "model not supported" message instead of 404. Our
    classifier already routes 400 → BadRequestError; this test pins
    that the deepseek error-body shape doesn't accidentally trip the
    quota branch."""
    body = json.dumps({
        "error": {
            "message": "Model deepseek-coder-6.7b is not supported.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }).encode()
    err = urllib.error.HTTPError(
        url="x", code=400, msg="bad", hdrs=None,
        fp=io.BytesIO(body),
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(
        api_key="k", model="deepseek-coder-6.7b", name="deepseek",
    )
    with pytest.raises(S.BadRequestError) as exc:
        prov.complete("sys", "user")
    # NOT classified as quota:
    assert not isinstance(exc.value, S.QuotaExhaustedError)
    assert "deepseek" in str(exc.value).lower()


# ----------------------------------------------------------------------
# URLError flavours
# ----------------------------------------------------------------------


def test_chat_url_error_timeout_shape_retries(monkeypatch):
    """urllib raises URLError(reason=timeout()) when the underlying
    socket times out. Our retry loop already catches URLError
    generally; this test pins that timeout-flavoured URLErrors
    aren't routed differently from DNS-flavoured ones."""
    err = urllib.error.URLError(socket.timeout("operation timed out"))
    urlopen = _make_urlopen([
        err,
        json.dumps({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode(),
    ])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.OpenAIChatProvider(api_key="k", model="m")
    text, _, _ = prov.complete("sys", "user")
    assert text == "ok"
    assert urlopen._state["calls"] == 2


def test_gemini_400_with_thinking_budget_error(monkeypatch):
    """Real-world Gemini 400 we've seen: 'Budget 0 is invalid for
    this model.' This is a deterministic input error (the request
    asks for thinking_budget=0 against gemini-2.5-pro which doesn't
    accept it). 400 → BadRequestError, no retry, distinct from
    quota."""
    body = json.dumps({
        "error": {
            "code": 400,
            "message": "Budget 0 is invalid. This model only "
                       "works in thinking mode.",
        }
    }).encode()
    err = urllib.error.HTTPError(
        url="x", code=400, msg="bad", hdrs=None,
        fp=io.BytesIO(body),
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    prov = S.GeminiProvider(api_key="k", model="gemini-2.5-pro")
    with pytest.raises(S.BadRequestError):
        prov.complete("sys", "user")
    assert urlopen._state["calls"] == 1
