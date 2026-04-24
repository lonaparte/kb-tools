"""Unit tests for embedding API response parsing robustness.

These tests exercise the production `embed()` path by monkeypatching
the underlying client object, so a regression in the wrapping logic
around `resp.data` / `resp.embeddings` will actually fail. (An
earlier version of this file tested a duplicated try/except in the
test body itself, which passed regardless of whether the production
code had the wrap — useless.)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kb_mcp.embedding import (
    EmbeddingError,
    OpenAIEmbeddingProvider,
    GeminiEmbeddingProvider,
)


class _FakeOpenAIClient:
    """Drop-in for `self._client` inside OpenAIEmbeddingProvider.

    `embeddings.create(...)` returns whatever `response` the test
    passed in. The production code then attempts
    `resp.data[i].embedding` and `resp.usage.prompt_tokens`; we
    verify those accesses are caught and re-raised as EmbeddingError.
    """
    def __init__(self, response):
        self._response = response
        self.embeddings = SimpleNamespace(create=self._create)

    def _create(self, *, model, input):
        return self._response


class _FakeGeminiClient:
    """Drop-in for Gemini's client. Exposes a `models.embed_content`
    method returning the configured response."""
    def __init__(self, response):
        self._response = response
        self.models = SimpleNamespace(embed_content=self._embed)

    def _embed(self, *, model, contents, config):
        return self._response


# ----------------------------------------------------------------------
# OpenAI provider
# ----------------------------------------------------------------------


def _openai_provider(monkeypatch, fake_response):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-for-response-robustness")
    prov = OpenAIEmbeddingProvider()
    # Replace the real OpenAI SDK client with our fake.
    prov._client = _FakeOpenAIClient(fake_response)
    return prov


def test_openai_missing_data_attribute_raises_embedding_error(monkeypatch):
    """A 200 response without a `data` attribute should surface as
    EmbeddingError (not AttributeError leaking from the list comp)."""
    bad_response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10))
    prov = _openai_provider(monkeypatch, bad_response)
    with pytest.raises(EmbeddingError) as exc:
        prov.embed(["hello"])
    assert "unexpected response format" in str(exc.value).lower()


def test_openai_data_item_missing_embedding_raises(monkeypatch):
    """Items in `resp.data` without `.embedding` → EmbeddingError."""
    bad_response = SimpleNamespace(
        data=[SimpleNamespace()],  # no .embedding
        usage=SimpleNamespace(prompt_tokens=10),
    )
    prov = _openai_provider(monkeypatch, bad_response)
    with pytest.raises(EmbeddingError) as exc:
        prov.embed(["hello"])
    assert "unexpected response format" in str(exc.value).lower()


def test_openai_good_response_succeeds(monkeypatch):
    """Sanity check: a well-formed response produces vectors."""
    good_response = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])],
        usage=SimpleNamespace(prompt_tokens=5),
    )
    prov = _openai_provider(monkeypatch, good_response)
    result = prov.embed(["hello"])
    assert result.vectors == [[0.1, 0.2, 0.3]]
    assert result.prompt_tokens == 5


def test_openai_empty_input_short_circuits(monkeypatch):
    """Empty input list must not hit the API — no client needed."""
    # Use a client that would error if called.
    prov = _openai_provider(monkeypatch, None)
    result = prov.embed([])
    assert result.vectors == []
    assert result.prompt_tokens == 0


# ----------------------------------------------------------------------
# Gemini provider
# ----------------------------------------------------------------------


def _gemini_provider(monkeypatch, fake_response):
    monkeypatch.setenv("GEMINI_API_KEY", "test-fake-for-response-robustness")
    # Gemini's constructor imports google.genai. Skip the test if
    # google-genai isn't installed in the dev venv.
    try:
        prov = GeminiEmbeddingProvider()
    except EmbeddingError as e:
        pytest.skip(f"google-genai not available: {e}")
    prov._client = _FakeGeminiClient(fake_response)
    return prov


def test_gemini_missing_embeddings_attribute_raises(monkeypatch):
    """Response lacking `.embeddings` → EmbeddingError."""
    bad_response = SimpleNamespace()  # no .embeddings
    prov = _gemini_provider(monkeypatch, bad_response)
    with pytest.raises(EmbeddingError) as exc:
        prov.embed(["hello"])
    assert "unexpected response format" in str(exc.value).lower()


def test_gemini_embedding_item_missing_values_raises(monkeypatch):
    """Items in `resp.embeddings` without `.values` → EmbeddingError."""
    bad_response = SimpleNamespace(
        embeddings=[SimpleNamespace()],  # no .values
    )
    prov = _gemini_provider(monkeypatch, bad_response)
    with pytest.raises(EmbeddingError) as exc:
        prov.embed(["hello"])
    assert "unexpected response format" in str(exc.value).lower()


def test_gemini_good_response_succeeds(monkeypatch):
    """Sanity: well-formed Gemini response produces vectors."""
    good_response = SimpleNamespace(
        embeddings=[
            SimpleNamespace(
                values=[0.1, 0.2, 0.3],
                statistics=SimpleNamespace(token_count=7),
            ),
        ],
    )
    prov = _gemini_provider(monkeypatch, good_response)
    result = prov.embed(["hello"])
    assert result.vectors == [[0.1, 0.2, 0.3]]
    assert result.prompt_tokens == 7


def test_gemini_missing_statistics_tolerated(monkeypatch):
    """Older Gemini SDKs don't expose per-embedding statistics.
    Code falls through to 0 tokens without raising."""
    response = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3])],  # no statistics
    )
    prov = _gemini_provider(monkeypatch, response)
    result = prov.embed(["hello"])
    assert result.vectors == [[0.1, 0.2, 0.3]]
    assert result.prompt_tokens == 0
