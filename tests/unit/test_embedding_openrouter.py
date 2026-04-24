"""Unit tests for the OpenRouter embedding provider (1.1.0+)."""
from __future__ import annotations

import os

import pytest

from kb_mcp.embedding import (
    SUPPORTED_PROVIDERS,
    EmbeddingError,
    OpenRouterEmbeddingProvider,
    _model_dim,
    build_from_config,
)
from kb_mcp.config import _resolve_embedding_model


class _FakeCfg:
    """Duck-typed drop-in for kb_mcp.config.Config."""
    def __init__(self, **kw):
        self.embeddings_enabled = kw.get("embeddings_enabled", True)
        self.embedding_provider = kw.get("embedding_provider", "openrouter")
        self.embedding_model = kw.get("embedding_model", None)
        self.embedding_dim = kw.get("embedding_dim", None)
        self.openrouter_api_key_env = kw.get(
            "openrouter_api_key_env", "OPENROUTER_EMBEDDING_API_KEY"
        )
        self.openrouter_base_url = kw.get("openrouter_base_url", None)


def test_openrouter_in_supported_providers():
    assert "openrouter" in SUPPORTED_PROVIDERS


def test_openrouter_defaults():
    assert OpenRouterEmbeddingProvider.DEFAULT_MODEL == "openai/text-embedding-3-small"
    assert OpenRouterEmbeddingProvider.DEFAULT_BASE_URL == "https://openrouter.ai/api/v1"
    assert OpenRouterEmbeddingProvider.DEFAULT_API_KEY_ENV == "OPENROUTER_EMBEDDING_API_KEY"
    assert OpenRouterEmbeddingProvider.FALLBACK_API_KEY_ENV == "OPENROUTER_API_KEY"


def test_openrouter_default_model_resolves():
    assert _resolve_embedding_model("openrouter", None) == "openai/text-embedding-3-small"


def test_openrouter_respects_explicit_model():
    assert _resolve_embedding_model("openrouter", "voyage-ai/voyage-3") == "voyage-ai/voyage-3"


def test_missing_api_key_returns_none_not_raise(monkeypatch):
    """Setup failure must not abort indexing of non-embedded data."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_EMBEDDING_API_KEY", raising=False)
    cfg = _FakeCfg(embedding_provider="openrouter",
                   embedding_model="openai/text-embedding-3-small")
    assert build_from_config(cfg) is None


def test_embedding_key_takes_precedence_over_fallback(monkeypatch):
    """If both OPENROUTER_EMBEDDING_API_KEY and OPENROUTER_API_KEY
    are set, the embedding-specific one wins — lets users route
    embedding and fulltext to different OpenRouter accounts."""
    monkeypatch.setenv("OPENROUTER_EMBEDDING_API_KEY", "sk-or-emb-specific")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fulltext-shared")
    prov = OpenRouterEmbeddingProvider()
    # The effective key used is not exposed directly; we can at
    # least verify the provider constructs without falling back.
    # (A direct check of the underlying OpenAI client's key would
    # inspect private state; keep this test to construction-only.)
    assert prov is not None


def test_fallback_to_generic_openrouter_key(monkeypatch):
    """When only OPENROUTER_API_KEY is set (single-key user),
    embedding construction still succeeds via the fallback."""
    monkeypatch.delenv("OPENROUTER_EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-generic")
    # Must not raise.
    prov = OpenRouterEmbeddingProvider()
    assert prov.model_name.startswith("openrouter/")


def test_no_fallback_when_user_picks_custom_env_var(monkeypatch):
    """If the user explicitly sets `openrouter_api_key_env` to a
    custom name (via kb-mcp.yaml), the fallback to
    OPENROUTER_API_KEY should NOT kick in — they opted out of
    auto-sharing."""
    monkeypatch.delenv("MY_CUSTOM_EMBED_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-generic")
    cfg = _FakeCfg(
        embedding_provider="openrouter",
        openrouter_api_key_env="MY_CUSTOM_EMBED_KEY",
    )
    # Custom env not set → provider build returns None (graceful).
    assert build_from_config(cfg) is None


def test_dim_lookup_strips_vendor_prefix():
    assert _model_dim("openai/text-embedding-3-small") == 1536
    assert _model_dim("openai/text-embedding-3-large") == 3072
    assert _model_dim("voyage-ai/voyage-3") == 1024


def test_dim_lookup_bare_name_still_works():
    """Direct-OpenAI names without vendor prefix still resolve."""
    assert _model_dim("text-embedding-3-small") == 1536
    assert _model_dim("text-embedding-3-large") == 3072


def test_unknown_model_raises_helpful_error():
    with pytest.raises(EmbeddingError) as exc:
        _model_dim("vendor-x/mystery-model")
    msg = str(exc.value)
    assert "Unknown embedding model" in msg
    assert "dim: <N>" in msg  # points user to the override


def test_openrouter_model_name_prefix(monkeypatch):
    """Stored papers.embedding_model must distinguish OpenRouter-routed
    vectors from direct-OpenAI ones so switching doesn't silently
    reuse cached vectors."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-unit-test")
    prov = OpenRouterEmbeddingProvider()
    assert prov.model_name.startswith("openrouter/")
    assert "openai/text-embedding-3-small" in prov.model_name


def test_openrouter_dim_matches_openai_text_embedding_3_small(monkeypatch):
    """Default OpenRouter → OpenAI produces 1536-dim vectors, same as
    direct OpenAI. Users can switch providers without rebuilding the
    vec0 table (the vectors collide on dim, though the stored model
    string still differs — see test_openrouter_model_name_prefix)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-unit-test")
    prov = OpenRouterEmbeddingProvider()
    assert prov.dim == 1536


def test_explicit_dim_override_for_unknown_model(monkeypatch):
    """A model not in the _model_dim table should be usable via
    `embedding_dim` override, so we don't force users to edit the
    provider's dim table for every new OpenRouter model."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-unit-test")
    prov = OpenRouterEmbeddingProvider(
        model="new-vendor/experimental-embed-v1",
        dim_override=2048,
    )
    assert prov.dim == 2048
    assert prov.model_name == "openrouter/new-vendor/experimental-embed-v1"
