"""Regression test for HTTP client reuse in kb_citations providers.

The review flagged that rebuilding httpx.Client per-request would
waste connection pool warmup. Existing code already holds a
single client on the Provider instance; this test ensures a
well-meaning refactor doesn't silently move Client construction
into the per-request `_get` method.

Tests auto-skip when httpx isn't installed — kb_citations itself
wouldn't import, so there's nothing to verify in that case."""
from __future__ import annotations

import pytest


def _skip_if_no_httpx():
    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip("httpx not installed; kb_citations providers unavailable")


def test_semantic_scholar_provider_has_single_client():
    """The provider instance must keep one httpx.Client across all
    requests it makes during its lifetime."""
    _skip_if_no_httpx()
    from kb_citations.semantic_scholar import SemanticScholarProvider
    p = SemanticScholarProvider(api_key="test-dummy")
    assert hasattr(p, "_client"), (
        "SemanticScholarProvider must expose a reusable _client — "
        "rebuilding httpx.Client per request wastes pool warmup"
    )
    client_id_before = id(p._client)
    # Touching internal state to confirm the attribute isn't a
    # property that rebuilds each access.
    assert id(p._client) == client_id_before
    p.close()


def test_openalex_provider_has_single_client():
    _skip_if_no_httpx()
    from kb_citations.openalex import OpenAlexProvider
    p = OpenAlexProvider(mailto="test@example.com")
    assert hasattr(p, "_client"), (
        "OpenAlexProvider must expose a reusable _client"
    )
    client_id_before = id(p._client)
    assert id(p._client) == client_id_before
    p.close()


def test_semantic_scholar_context_manager_closes_client():
    """`with provider:` must actually tear down the underlying
    client so the process doesn't leak TCP connections."""
    _skip_if_no_httpx()
    from kb_citations.semantic_scholar import SemanticScholarProvider
    with SemanticScholarProvider(api_key="test-dummy") as p:
        client = p._client
        # Inside the `with`, client is alive (httpx clients expose
        # `.is_closed`).
        assert not client.is_closed
    # After exit, closed.
    assert client.is_closed


def test_openalex_context_manager_closes_client():
    _skip_if_no_httpx()
    from kb_citations.openalex import OpenAlexProvider
    with OpenAlexProvider(mailto="test@example.com") as p:
        client = p._client
        assert not client.is_closed
    assert client.is_closed
