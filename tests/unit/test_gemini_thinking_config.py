"""Regression for the v0.28.1 gemini thinkingConfig fix.

Pre-0.28.1 the GeminiProvider used a blanket rule: any
`gemini-2.5-*` model got `thinkingConfig: {"thinkingBudget": 0}`.
That's valid for `gemini-2.5-flash` / `-flash-lite` (which accept 0
to disable thinking) but INVALID for `gemini-2.5-pro`, which
rejects 0 with "Budget 0 is invalid. This model only works in
thinking mode." — a 400 that isn't retryable.

The practical impact: `--fulltext-fallback-model` defaults to
`gemini-2.5-pro` (it has a much larger RPD than the primary
3.1-pro-preview). Once the primary runs out of quota and we switch,
EVERY subsequent paper hits the 400 and is counted as llm-fail.
Hundreds of papers stuck in that state was the v0.28.0 field
report.

This test stubs urllib.request.urlopen, inspects the request body
the provider actually sends, and asserts the correct thinkingConfig
for each documented variant. Doesn't need a real API key or network.
"""
from __future__ import annotations

import json
from unittest.mock import patch


class _FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self):
        return json.dumps({
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 1,
            },
        }).encode()


def _capture_request_body(model: str) -> dict:
    """Send one complete() call through a stubbed urlopen and return
    the parsed JSON body that would have been POSTed."""
    from kb_importer.summarize import GeminiProvider

    captured: dict = {}

    def fake_urlopen(req, timeout=120):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse()

    prov = GeminiProvider(api_key="fake-key", model=model)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        prov.complete("sys prompt", "user prompt")
    return captured["body"]


def _thinking_config(model: str):
    return _capture_request_body(model)["generationConfig"].get("thinkingConfig")


def test_gemini_3x_uses_thinking_level_low():
    """3.x family (incl. 3.1-pro-preview, 3-pro-preview,
    3.1-flash-lite, 3.1-flash-preview) → thinkingLevel='low'."""
    for model in [
        "gemini-3.1-pro-preview",
        "gemini-3-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    ]:
        tc = _thinking_config(model)
        assert tc == {"thinkingLevel": "low"}, (
            f"{model}: got thinkingConfig={tc!r}, expected thinkingLevel=low"
        )


def test_gemini_25_flash_gets_budget_zero():
    """flash and flash-lite accept thinkingBudget=0 (disables
    thinking). Keep the original minimise-overhead behaviour."""
    for model in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        tc = _thinking_config(model)
        assert tc == {"thinkingBudget": 0}, (
            f"{model}: got thinkingConfig={tc!r}, expected thinkingBudget=0"
        )


def test_gemini_25_pro_does_not_get_budget_zero():
    """The critical regression. 2.5-pro rejects thinkingBudget=0; we
    must send a positive value (or -1 for dynamic) instead. Using
    128 is the documented minimum for this model — keeps thinking
    overhead minimal while respecting the API's contract."""
    tc = _thinking_config("gemini-2.5-pro")
    assert tc is not None, (
        "2.5-pro with no thinkingConfig falls to Google's default "
        "(large dynamic budget) — that eats output tokens. Set "
        "an explicit small budget instead."
    )
    assert "thinkingBudget" in tc, (
        f"2.5-pro should use thinkingBudget (not level); got {tc!r}"
    )
    budget = tc["thinkingBudget"]
    assert budget != 0, (
        f"2.5-pro must NOT send thinkingBudget=0 — the API rejects "
        f"with HTTP 400 'Budget 0 is invalid. This model only works "
        f"in thinking mode.' Got budget={budget}."
    )
    # Either -1 (dynamic) or a positive int (>=128 per Google's
    # documented minimum) is valid. Reject anything weird.
    assert budget == -1 or budget >= 128, (
        f"2.5-pro thinkingBudget must be -1 or >=128; got {budget}"
    )


def test_gemini_20_family_omits_thinking_config():
    """2.0 and older don't support thinking — we must NOT send the
    key (Google treats unknown keys as a 400)."""
    assert _thinking_config("gemini-2.0-flash") is None
    assert _thinking_config("gemini-1.5-pro") is None


def test_unknown_25_variant_stays_safe():
    """Any hypothetical future gemini-2.5-* (beyond pro/flash/
    flash-lite) should NOT accidentally get thinkingBudget=0 — that
    was the footgun that bit 2.5-pro. Our fallback path sends the
    safe positive minimum."""
    tc = _thinking_config("gemini-2.5-experimental-new-variant")
    assert tc is not None
    assert tc.get("thinkingBudget") != 0, (
        "unknown 2.5-* variant must default to the 2.5-pro-safe "
        "positive budget, not the flash-only 0"
    )
