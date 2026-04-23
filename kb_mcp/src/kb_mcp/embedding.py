"""Embedding providers.

One `EmbeddingProvider` protocol, one `OpenAIEmbeddingProvider`
implementation. Future providers (Ollama, Cohere, local) slot in
behind the same interface.

Phase 2b ships only OpenAI because the user's deployment target is a
cloud server without GPU. Switching model/provider is a config
change — no indexer changes needed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Any problem producing embeddings (API failure, missing key, etc.)."""


@dataclass
class EmbeddingResult:
    """Vectors for a batch of inputs plus usage metadata."""
    vectors: list[list[float]]
    model: str
    # Sum of tokens across all texts in this batch, as reported by the
    # API. Useful for cost accounting in logs.
    prompt_tokens: int


class EmbeddingProvider(Protocol):
    """Minimal interface for text → vector."""

    @property
    def dim(self) -> int:
        """Dimensionality of the vectors this provider returns."""
        ...

    @property
    def model_name(self) -> str:
        """Stable identifier used in papers.embedding_model column."""
        ...

    def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a batch of texts.

        Implementations should handle their own rate limiting and
        retries. Raises EmbeddingError on unrecoverable failure.

        Empty list input → empty vectors list (no API call).
        """
        ...


class OpenAIEmbeddingProvider:
    """Embeddings via OpenAI's /v1/embeddings endpoint.

    Default model: text-embedding-3-small (1536 dim). You can pass
    text-embedding-3-large for higher quality (3072 dim, ~6× cost) —
    but then you need to DROP the paper_chunks_vec table and rebuild
    because the vec0 dimension is compile-time fixed in the schema.

    Rate limits: OpenAI's client SDK handles 429 + transient errors
    with automatic retry. We don't add our own layer.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
    ):
        self._model = model
        self._dim = _model_dim(model)

        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise EmbeddingError(
                f"OpenAI API key not found in environment variable "
                f"{api_key_env!r}. Set it to enable embeddings."
            )

        try:
            from openai import OpenAI
        except ImportError as e:
            raise EmbeddingError(
                "openai package not installed. "
                "Run `pip install openai`."
            ) from e

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self._model, prompt_tokens=0)

        # OpenAI limits: max 2048 inputs/call for text-embedding-3-*,
        # max ~8192 tokens per input. We let the caller worry about
        # batch size (indexer does this); this layer just sends what
        # it gets and relies on the client SDK for retry.
        try:
            resp = self._client.embeddings.create(
                model=self._model,
                input=texts,
            )
        except Exception as e:
            raise EmbeddingError(
                f"OpenAI embeddings API call failed: {e}"
            ) from e

        # Response is shape {data: [{embedding: [...]}, ...], usage: {...}}
        vectors = [d.embedding for d in resp.data]
        prompt_tokens = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
        return EmbeddingResult(
            vectors=vectors,
            model=self._model,
            prompt_tokens=prompt_tokens,
        )


def _model_dim(model: str) -> int:
    """Known output dimensions per OpenAI model.

    Not using the /models endpoint because that requires an API call
    at construction time, and we want to fail fast on unknown models.
    """
    dims = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,   # legacy
    }
    if model not in dims:
        raise EmbeddingError(
            f"Unknown model {model!r}. Known: {list(dims)}. "
            f"If this is a custom-compatible endpoint, extend _model_dim."
        )
    return dims[model]


class GeminiEmbeddingProvider:
    """Embeddings via Google's Gemini API.

    Model: gemini-embedding-001 (3072 dim, configurable via
    `output_dimensionality` on the call). Here we use 1536 by default
    to match the openai text-embedding-3-small dimension, so users
    can switch providers without rebuilding the vec0 table.

    Requires `google-genai` (the new unified SDK, not the older
    `google-generativeai`).
    """

    # Reduced embedding dimension. Gemini supports MRL truncation,
    # so we ask for 1536 and get a correctly-normalized prefix.
    # Matching OpenAI's default dim lets users swap providers without
    # DROP TABLE paper_chunks_vec + reindex.
    DEFAULT_DIM = 1536

    def __init__(
        self,
        model: str = "gemini-embedding-001",
        api_key_env: str = "GEMINI_API_KEY",
        output_dim: int | None = None,
    ):
        self._model = model
        self._dim = output_dim if output_dim is not None else self.DEFAULT_DIM

        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise EmbeddingError(
                f"Gemini API key not found in environment variable "
                f"{api_key_env!r}. Set it in your shell rc to enable."
            )

        try:
            from google import genai
        except ImportError as e:
            raise EmbeddingError(
                "google-genai package not installed. "
                "Run `pip install google-genai`."
            ) from e

        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        # Append dim to stored model name so re-embedding with a
        # different MRL setting counts as a different model in
        # papers.embedding_model — avoids mixing incompatible vectors.
        return f"{self._model}@{self._dim}"

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model_name, prompt_tokens=0)

        # Gemini's embed_content takes a list (or single) of contents.
        # Batch limits: 100 inputs/call, ~2048 tokens each. Indexer
        # is expected to respect this.
        try:
            from google.genai import types as genai_types
            cfg = genai_types.EmbedContentConfig(
                output_dimensionality=self._dim,
            )
            resp = self._client.models.embed_content(
                model=self._model,
                contents=texts,
                config=cfg,
            )
        except Exception as e:
            raise EmbeddingError(
                f"Gemini embeddings API call failed: {e}"
            ) from e

        # Response: {embeddings: [{values: [...], statistics:
        # {token_count: N, truncated: bool}}, ...]}
        # Each embedding carries per-content stats. Sum them for the
        # batch total. Gracefully handle older SDK versions that
        # don't expose statistics (falls back to 0, same as before).
        vectors = [list(e.values) for e in resp.embeddings]
        total_tokens = 0
        for e in resp.embeddings:
            stats = getattr(e, "statistics", None)
            if stats is not None:
                tc = getattr(stats, "token_count", None)
                if tc is not None:
                    try:
                        total_tokens += int(tc)
                    except (TypeError, ValueError):
                        pass
        return EmbeddingResult(
            vectors=vectors,
            model=self.model_name,
            prompt_tokens=total_tokens,
        )


# Names accepted by `build_from_config(cfg)` and by the
# `kb-mcp reindex --provider X` CLI choice. Adding a new provider
# means: (1) implement its class here, (2) add its name here,
# (3) dispatch in build_from_config below. No other files change.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai", "gemini")


def build_from_config(cfg) -> EmbeddingProvider | None:
    """Construct a provider from Config, or return None if disabled.

    None means "don't run embedding steps at all" — indexer will
    skip them and set embedded=0. A missing API key also returns
    None (with a warning) so setup problems don't abort indexing
    of non-embedded data.

    Supported providers: see SUPPORTED_PROVIDERS constant below.

    Both current providers produce 1536-dim vectors by default, so
    you can switch providers without rebuilding paper_chunks_vec
    from zero. The stored `embedding_model` column distinguishes
    them, so `kb-mcp index` will re-embed any paper whose stored
    model differs from the current provider.
    """
    if not getattr(cfg, "embeddings_enabled", True):
        return None

    provider_name = getattr(cfg, "embedding_provider", "openai").lower()

    if provider_name == "openai":
        try:
            return OpenAIEmbeddingProvider(
                model=getattr(cfg, "embedding_model", "text-embedding-3-small"),
                api_key_env=getattr(cfg, "openai_api_key_env", "OPENAI_API_KEY"),
                base_url=getattr(cfg, "openai_base_url", None),
            )
        except EmbeddingError as e:
            log.warning("OpenAI embedding provider unavailable: %s", e)
            return None

    if provider_name == "gemini":
        try:
            return GeminiEmbeddingProvider(
                model=getattr(cfg, "embedding_model", "gemini-embedding-001"),
                api_key_env=getattr(cfg, "gemini_api_key_env", "GEMINI_API_KEY"),
                output_dim=getattr(cfg, "embedding_dim", None),
            )
        except EmbeddingError as e:
            log.warning("Gemini embedding provider unavailable: %s", e)
            return None

    raise EmbeddingError(
        f"Unknown embedding_provider {provider_name!r}. "
        f"Supported: 'openai', 'gemini'."
    )
