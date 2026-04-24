"""Unit tests for OpenRouter support in the fulltext summarizer
(kb-importer `--fulltext-provider openrouter`, 1.2.0+).

The embedding side is covered by tests/unit/test_embedding_openrouter.py.
These tests cover the distinct fulltext-summary provider factory and
the `OPENROUTER_API_KEY` env var that it uses (separate from the
embedding side's `OPENROUTER_EMBEDDING_API_KEY`).
"""
from __future__ import annotations

import pytest

from kb_importer.summarize import (
    SummarizerError,
    OpenAIChatProvider,
    build_provider_from_env,
)


def test_openrouter_accepted_as_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")
    prov = build_provider_from_env("openrouter")
    assert isinstance(prov, OpenAIChatProvider)
    assert prov.name == "openrouter"
    # 1.2.1 default: free-tier open-weight model on OpenRouter.
    # Capability may lag paid models; users should override via
    # --fulltext-model for important libraries.
    assert prov.model == "openai/gpt-oss-120b:free"


def test_openrouter_respects_explicit_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")
    prov = build_provider_from_env(
        "openrouter",
        model="anthropic/claude-sonnet-4.5",
    )
    assert prov.model == "anthropic/claude-sonnet-4.5"


def test_openrouter_missing_key_surfaces_helpful_error(monkeypatch):
    """No key → clear error pointing to the OpenRouter keys page.
    Matches the pattern used for the other providers."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SummarizerError) as exc:
        build_provider_from_env("openrouter")
    msg = str(exc.value)
    assert "OPENROUTER_API_KEY" in msg
    assert "openrouter.ai/keys" in msg


def test_openrouter_does_not_read_embedding_key(monkeypatch):
    """Fulltext side must NOT silently pick up the embedding-side
    env var (OPENROUTER_EMBEDDING_API_KEY). The two are deliberately
    separate; sharing would break the 'two keys, two accounts' use
    case that motivated the split."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_EMBEDDING_API_KEY", "sk-or-emb-only")
    with pytest.raises(SummarizerError):
        build_provider_from_env("openrouter")


def test_openrouter_wire_is_openai_compatible(monkeypatch):
    """The OpenRouter provider is just OpenAIChatProvider with a
    base_url override — same request shape, same response parser.
    This is a construction-time check that the factory wired the
    right base URL."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")
    prov = build_provider_from_env("openrouter")
    assert prov._base_url.rstrip("/") == "https://openrouter.ai/api/v1"


def test_openrouter_sets_attribution_headers(monkeypatch):
    """OpenRouter's optional HTTP-Referer / X-Title headers identify
    the caller on their public leaderboard. They're not required
    for auth, but we set sensible defaults to surface ee-kb-tools
    usage."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")
    prov = build_provider_from_env("openrouter")
    assert "HTTP-Referer" in prov._extra_headers
    assert "X-Title" in prov._extra_headers
    assert "ee-kb-tools" in prov._extra_headers["X-Title"]


def test_unknown_provider_message_lists_openrouter():
    """The error message for an unknown provider should advertise
    openrouter as a supported choice (caught by reviewers after
    the classifier Beta→Stable pass)."""
    with pytest.raises(SummarizerError) as exc:
        build_provider_from_env("gpt5")
    msg = str(exc.value)
    assert "openrouter" in msg
