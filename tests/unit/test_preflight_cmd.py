"""Unit tests for the kb-importer preflight subcommand.

preflight pings the LLM provider with a tiny request. Tests here
verify CLI wiring, error paths, and that config isn't required —
a user should be able to run `kb-importer preflight` before their
Zotero setup is complete.

Uses manual stdout/stderr redirection rather than pytest's capsys
fixture so the stdlib test runner (scripts/run_unit_tests.py) can
execute these.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error

import pytest

from kb_importer import summarize as S
from kb_importer.commands.preflight_cmd import _cmd_preflight


class _Args:
    """argparse-style namespace."""
    def __init__(self, **kw):
        self.fulltext_provider = kw.get("fulltext_provider", "gemini")
        self.fulltext_model = kw.get("fulltext_model", None)
        self.prompt = kw.get("prompt", "reply with just the word 'ok'")
        self.max_tokens = kw.get("max_tokens", 10)


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


class _Capture:
    """Minimal stdout/stderr capture for the stdlib test runner."""
    def __init__(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self._orig_out = None
        self._orig_err = None
    def __enter__(self):
        self._orig_out = sys.stdout
        self._orig_err = sys.stderr
        sys.stdout = self.out
        sys.stderr = self.err
        return self
    def __exit__(self, *a):
        sys.stdout = self._orig_out
        sys.stderr = self._orig_err
        return False


def test_preflight_no_api_key_returns_nonzero(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with _Capture() as cap:
        rc = _cmd_preflight(_Args(fulltext_provider="openai"), cfg=None)
    assert rc == 2
    err = cap.err.getvalue()
    assert "preflight" in err
    assert "OPENAI_API_KEY" in err


def test_preflight_happy_path_returns_zero(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    resp = json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }).encode("utf-8")
    urlopen = _make_urlopen([resp])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    with _Capture() as cap:
        rc = _cmd_preflight(_Args(fulltext_provider="openai"), cfg=None)
    assert rc == 0
    out = cap.out.getvalue()
    assert "✓" in out


def test_preflight_quota_returns_3(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    err = urllib.error.HTTPError(
        url="x", code=429, msg="quota",
        hdrs=None,
        fp=io.BytesIO(b"rate_limit_exceeded: try again in 60s"),
    )
    err.headers = type("H", (), {"get": lambda self, k, d=None: None})()
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    with _Capture() as cap:
        rc = _cmd_preflight(_Args(fulltext_provider="openai"), cfg=None)
    assert rc == 3
    assert "quota" in cap.err.getvalue().lower()


def test_preflight_bad_request_returns_4(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    err = urllib.error.HTTPError(
        url="x", code=400, msg="bad", hdrs=None,
        fp=io.BytesIO(b"prompt too long"),
    )
    urlopen = _make_urlopen([err])
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", urlopen)

    with _Capture() as cap:
        rc = _cmd_preflight(_Args(fulltext_provider="openai"), cfg=None)
    assert rc == 4
    err_out = cap.err.getvalue()
    assert "rejected" in err_out.lower() or "400" in err_out
    assert "model" in err_out.lower()
