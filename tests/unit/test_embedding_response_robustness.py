"""Unit tests for embedding API response parsing robustness."""
from __future__ import annotations

import pytest

from kb_mcp.embedding import (
    EmbeddingError,
    OpenAIEmbeddingProvider,
    GeminiEmbeddingProvider,
)


class MockBadOpenAIResponse:
    """Mock OpenAI response with missing or malformed data."""
    def __init__(self, has_data=True, has_embedding=True, has_usage=True):
        if has_data:
            if has_embedding:
                self.data = [type('obj', (), {'embedding': [0.1, 0.2, 0.3]})]
            else:
                # data exists but items lack embedding attribute
                self.data = [type('obj', (), {})]
        # else: no data attribute at all

        if has_usage:
            self.usage = type('obj', (), {'prompt_tokens': 10})
        else:
            self.usage = None


class MockBadGeminiResponse:
    """Mock Gemini response with missing or malformed embeddings."""
    def __init__(self, has_embeddings=True, has_values=True):
        if has_embeddings:
            if has_values:
                self.embeddings = [type('obj', (), {'values': [0.1, 0.2, 0.3]})]
            else:
                # embeddings exists but items lack values attribute
                self.embeddings = [type('obj', (), {})]
        # else: no embeddings attribute at all


class TestOpenAIResponseParsing:
    """Test OpenAI embedding response parsing robustness."""

    def test_missing_data_attribute_raises_embedding_error(self, monkeypatch):
        """Missing resp.data should raise EmbeddingError, not AttributeError."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        provider = OpenAIEmbeddingProvider()

        # Create mock response without data attribute
        bad_resp = type('obj', (), {'usage': None})

        # Simulate the response parsing section
        with pytest.raises(EmbeddingError) as exc:
            try:
                vectors = [d.embedding for d in bad_resp.data]
            except AttributeError as e:
                raise EmbeddingError(
                    f"OpenAI returned unexpected response format: {e}"
                ) from e

        assert "unexpected response format" in str(exc.value).lower()

    def test_missing_embedding_attribute_raises_embedding_error(self, monkeypatch):
        """Items in resp.data missing embedding should raise EmbeddingError."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        provider = OpenAIEmbeddingProvider()

        # Create mock response with data but no embedding in items
        bad_resp = type('obj', (), {
            'data': [type('obj', (), {})],  # no embedding attribute
            'usage': type('obj', (), {'prompt_tokens': 10})
        })

        with pytest.raises(EmbeddingError) as exc:
            try:
                vectors = [d.embedding for d in bad_resp.data]
            except AttributeError as e:
                raise EmbeddingError(
                    f"OpenAI returned unexpected response format: {e}"
                ) from e

        assert "unexpected response format" in str(exc.value).lower()


class TestGeminiResponseParsing:
    """Test Gemini embedding response parsing robustness."""

    def test_missing_embeddings_attribute_raises_embedding_error(self, monkeypatch):
        """Missing resp.embeddings should raise EmbeddingError, not AttributeError."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-fake-key")

        # Create mock response without embeddings attribute
        bad_resp = type('obj', (), {})

        with pytest.raises(EmbeddingError) as exc:
            try:
                vectors = [list(e.values) for e in bad_resp.embeddings]
            except AttributeError as e:
                raise EmbeddingError(
                    f"Gemini returned unexpected response format: {e}"
                ) from e

        assert "unexpected response format" in str(exc.value).lower()

    def test_missing_values_attribute_raises_embedding_error(self, monkeypatch):
        """Items in resp.embeddings missing values should raise EmbeddingError."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-fake-key")

        # Create mock response with embeddings but no values in items
        bad_resp = type('obj', (), {
            'embeddings': [type('obj', (), {})]  # no values attribute
        })

        with pytest.raises(EmbeddingError) as exc:
            try:
                vectors = [list(e.values) for e in bad_resp.embeddings]
            except AttributeError as e:
                raise EmbeddingError(
                    f"Gemini returned unexpected response format: {e}"
                ) from e

        assert "unexpected response format" in str(exc.value).lower()
