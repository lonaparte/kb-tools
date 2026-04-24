"""Unit tests for `embeddings.dim` override on the OpenAI provider.

Motivation: the scaffold kb-mcp.yaml documents self-hosted
OpenAI-compatible gateways (Ollama, vLLM, LocalAI, DashScope) with
model names outside kb_mcp's built-in `_model_dim` table — e.g.
`nomic-embed-text` (768 dim), `BAAI/bge-large-en-v1.5` (1024 dim).
The scaffold says: set `dim: 768` in YAML to tell kb-mcp the
dimension.

Pre-fix, `build_from_config` did NOT forward `cfg.embedding_dim` to
`OpenAIEmbeddingProvider`, so `_model_dim(model)` fired on the
unknown model name and construction failed with EmbeddingError
("Unknown embedding model"). The scaffold example was documented-
but-not-working.

These tests lock in the working path so a future refactor doesn't
regress it.
"""
from __future__ import annotations

import pytest

from kb_mcp.embedding import (
    EmbeddingError,
    OpenAIEmbeddingProvider,
    build_from_config,
)


class _FakeCfg:
    def __init__(self, **kw):
        self.embeddings_enabled = kw.get("embeddings_enabled", True)
        self.embedding_provider = kw.get("embedding_provider", "openai")
        self.embedding_model = kw.get("embedding_model")
        self.embedding_dim = kw.get("embedding_dim")
        self.openai_api_key_env = kw.get(
            "openai_api_key_env", "OPENAI_API_KEY"
        )
        self.openai_base_url = kw.get("openai_base_url")


def test_openai_unknown_model_with_explicit_dim_succeeds(monkeypatch):
    """Scaffold-documented case: Ollama / vLLM hosts a non-OpenAI
    model via OpenAI-compatible wire. User sets `model:
    nomic-embed-text` + `dim: 768`. Pre-fix: crashed with
    `Unknown embedding model 'nomic-embed-text'`. Post-fix: builds
    successfully, provider reports dim=768."""
    monkeypatch.setenv("OPENAI_API_KEY", "ollama-dummy")
    cfg = _FakeCfg(
        embedding_provider="openai",
        embedding_model="nomic-embed-text",
        embedding_dim=768,
        openai_base_url="http://localhost:11434/v1",
    )
    prov = build_from_config(cfg)
    assert prov is not None
    assert prov.dim == 768
    assert prov.model_name == "nomic-embed-text"


def test_openai_unknown_model_without_dim_still_fails(monkeypatch):
    """The dim-override path requires the user to set dim explicitly.
    If they set an unknown model but NO dim, `_model_dim` still
    fires and the provider unavailable. Behavior documented —
    matches the scaffold guidance 'Pass `embeddings.dim: <N>` …
    for a model not in this table'."""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    cfg = _FakeCfg(
        embedding_provider="openai",
        embedding_model="unknown-model-v99",
        embedding_dim=None,
    )
    # build_from_config catches EmbeddingError and returns None.
    assert build_from_config(cfg) is None


def test_openai_known_model_no_dim_still_works(monkeypatch):
    """Regression check: the standard path (direct OpenAI with a
    known model, no explicit dim) still uses the built-in table."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    cfg = _FakeCfg(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dim=None,
    )
    prov = build_from_config(cfg)
    assert prov is not None
    assert prov.dim == 1536


def test_openai_known_model_dim_override_wins(monkeypatch):
    """If the user sets an unusual dim for a known model (e.g.
    using text-embedding-3-large at a non-default truncated dim),
    the override wins over the table default."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    cfg = _FakeCfg(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dim=512,  # explicit truncation
    )
    prov = build_from_config(cfg)
    assert prov is not None
    assert prov.dim == 512
