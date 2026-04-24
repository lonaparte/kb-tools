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

    0.29.8+: `base_url` override also lets this class talk to any
    OpenAI-compatible endpoint (Ollama, vLLM, LocalAI, etc.). For
    OpenRouter specifically, prefer `OpenRouterEmbeddingProvider`
    below — same wire protocol, but separate env var and a different
    default model naming convention (`<vendor>/<model>`).
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        *,
        dim_override: int | None = None,
    ):
        self._model = model
        # Most call sites resolve dim from the known-model table. The
        # OpenRouter subclass passes its own vendor-prefixed name and
        # sometimes a user-supplied dim (for models we haven't tabled).
        self._dim = dim_override if dim_override is not None else _model_dim(model)

        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise EmbeddingError(
                f"{self._provider_label()} API key not found in "
                f"environment variable {api_key_env!r}. "
                f"Set it to enable embeddings."
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

    def _provider_label(self) -> str:
        """Used in error messages. Overridden by subclasses."""
        return "OpenAI"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model_name, prompt_tokens=0)

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
                f"{self._provider_label()} embeddings API call failed: {e}"
            ) from e

        # Response is shape {data: [{embedding: [...]}, ...], usage: {...}}
        try:
            vectors = [d.embedding for d in resp.data]
            prompt_tokens = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
        except (AttributeError, TypeError) as e:
            raise EmbeddingError(
                f"{self._provider_label()} returned unexpected response format: {e}"
            ) from e
        return EmbeddingResult(
            vectors=vectors,
            model=self.model_name,
            prompt_tokens=prompt_tokens,
        )


class OpenRouterEmbeddingProvider(OpenAIEmbeddingProvider):
    """Embeddings via OpenRouter's OpenAI-compatible endpoint.

    OpenRouter (https://openrouter.ai) routes requests to many
    upstream embedding providers (OpenAI, Voyage, Cohere, BGE-hosted,
    etc.) behind one API key. It speaks the OpenAI /v1/embeddings
    wire format, so we subclass OpenAIEmbeddingProvider; the only
    differences are:

    - Default base URL: `https://openrouter.ai/api/v1`
    - Default env var: `OPENROUTER_EMBEDDING_API_KEY` with fallback
      to `OPENROUTER_API_KEY`. Rationale: kb-importer's fulltext
      pipeline uses `OPENROUTER_API_KEY`; splitting the embedding
      key lets users point the two pipelines at different
      OpenRouter accounts (e.g. one billed per-project, one per-
      person) — but single-key users who only set
      `OPENROUTER_API_KEY` get both working for free via the
      fallback.
    - Model IDs use OpenRouter's `<vendor>/<model>` naming, e.g.
      `openai/text-embedding-3-small` or `voyage-ai/voyage-3`.

    The stored `papers.embedding_model` column reflects the full
    routed name so vectors produced via OpenRouter don't collide
    with vectors from a direct OpenAI call — prevents accidental
    cache hits when routing is changed mid-library.

    See kb-mcp.yaml scaffold for how to configure this provider.
    """

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "openai/text-embedding-3-small"
    # Embedding-specific key tried first; falls back to the shared
    # OpenRouter key if the specific one isn't in the environment.
    DEFAULT_API_KEY_ENV = "OPENROUTER_EMBEDDING_API_KEY"
    FALLBACK_API_KEY_ENV = "OPENROUTER_API_KEY"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str | None = None,
        *,
        dim_override: int | None = None,
    ):
        # If the specific embedding-key env var is unset but the
        # general OPENROUTER_API_KEY is, silently use it. This is
        # the "I only have one OpenRouter key" path, which we want
        # to be zero-friction. Users who explicitly set
        # api_key_env to something else (via kb-mcp.yaml) skip the
        # fallback — they opted out of auto-sharing.
        effective_env = api_key_env
        if (
            api_key_env == self.DEFAULT_API_KEY_ENV
            and not os.environ.get(api_key_env, "").strip()
            and os.environ.get(self.FALLBACK_API_KEY_ENV, "").strip()
        ):
            log.info(
                "%s not set; using %s (fallback) for OpenRouter embedding",
                self.DEFAULT_API_KEY_ENV, self.FALLBACK_API_KEY_ENV,
            )
            effective_env = self.FALLBACK_API_KEY_ENV

        super().__init__(
            model=model,
            api_key_env=effective_env,
            base_url=base_url or self.DEFAULT_BASE_URL,
            dim_override=dim_override,
        )

    def _provider_label(self) -> str:
        return "OpenRouter"

    @property
    def model_name(self) -> str:
        # Prefix with the routing layer so the stored identifier is
        # unambiguous in papers.embedding_model. A subsequent switch
        # between direct OpenAI and OpenRouter-routed-OpenAI then
        # triggers a re-embed (dim is the same, but we don't want to
        # silently mix wire paths that could diverge over time).
        return f"openrouter/{self._model}"


def _model_dim(model: str) -> int:
    """Known output dimensions for embedding models.

    Accepts both bare names (e.g. `text-embedding-3-small`) and
    vendor-prefixed OpenRouter names (`openai/text-embedding-3-small`).

    Not using the /models endpoint because that requires an API call
    at construction time, and we want to fail fast on unknown models.
    """
    dims = {
        # OpenAI (direct and via OpenRouter)
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,   # legacy
        # Voyage AI via OpenRouter (common non-OpenAI option).
        # Known output sizes per provider docs.
        "voyage-3": 1024,
        "voyage-3-lite": 512,
        "voyage-3-large": 1024,
        # BGE / BAAI (sometimes offered via OpenRouter-compatible
        # self-hosted gateways).
        "bge-large-en-v1.5": 1024,
        "bge-base-en-v1.5": 768,
        "bge-small-en-v1.5": 384,
    }
    # OpenRouter model IDs have the form `<vendor>/<model>`.
    # Strip the vendor prefix for the lookup — the dimension depends
    # on the underlying model, not the routing layer.
    bare = model.split("/", 1)[1] if "/" in model else model
    if bare not in dims:
        raise EmbeddingError(
            f"Unknown embedding model {model!r} (looked up {bare!r}). "
            f"Known: {sorted(dims)}. "
            f"Pass `embeddings.dim: <N>` in kb-mcp.yaml to override "
            f"for a model not in this table, or extend _model_dim."
        )
    return dims[bare]


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
        try:
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
        except (AttributeError, TypeError) as e:
            raise EmbeddingError(
                f"Gemini returned unexpected response format: {e}"
            ) from e
        return EmbeddingResult(
            vectors=vectors,
            model=self.model_name,
            prompt_tokens=total_tokens,
        )


# Names accepted by `build_from_config(cfg)` and by the
# `kb-mcp reindex --provider X` CLI choice. Adding a new provider
# means: (1) implement its class here, (2) add its name here,
# (3) dispatch in build_from_config below. No other files change.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai", "gemini", "openrouter")


def build_from_config(cfg) -> EmbeddingProvider | None:
    """Construct a provider from Config, or return None if disabled.

    None means "don't run embedding steps at all" — indexer will
    skip them and set embedded=0. A missing API key also returns
    None (with a warning) so setup problems don't abort indexing
    of non-embedded data.

    Supported providers: see `SUPPORTED_PROVIDERS` constant above.

    All providers produce 1536-dim vectors by default, so you can
    switch providers without rebuilding paper_chunks_vec from zero.
    The stored `embedding_model` column distinguishes them, so
    `kb-mcp index` will re-embed any paper whose stored model
    differs from the current provider.

    Note on scope: this provider configuration controls **only the
    RAG / vector-index embedding pipeline**. The LLM that writes
    paper summaries during `kb-importer --fulltext` is a completely
    separate setup — see `kb_importer.summarize.build_provider_from_env`
    and the `--fulltext-provider` / `--fulltext-model` CLI flags.
    """
    if not getattr(cfg, "embeddings_enabled", True):
        return None

    provider_name = getattr(cfg, "embedding_provider", "openai").lower()

    # `getattr(..., default)` returns the default only when the
    # attribute is MISSING. load_config always sets
    # `cfg.embedding_model` but it may be None (user left `model:`
    # out of the YAML and _resolve_embedding_model returned None
    # for a newly-added provider before the dispatch table caught
    # up). Use `getattr(...) or <fallback>` to catch both cases.
    if provider_name == "openai":
        try:
            return OpenAIEmbeddingProvider(
                model=(getattr(cfg, "embedding_model", None)
                       or "text-embedding-3-small"),
                api_key_env=getattr(cfg, "openai_api_key_env", "OPENAI_API_KEY"),
                base_url=getattr(cfg, "openai_base_url", None),
            )
        except EmbeddingError as e:
            log.warning("OpenAI embedding provider unavailable: %s", e)
            return None

    if provider_name == "gemini":
        try:
            return GeminiEmbeddingProvider(
                model=(getattr(cfg, "embedding_model", None)
                       or "gemini-embedding-001"),
                api_key_env=getattr(cfg, "gemini_api_key_env", "GEMINI_API_KEY"),
                output_dim=getattr(cfg, "embedding_dim", None),
            )
        except EmbeddingError as e:
            log.warning("Gemini embedding provider unavailable: %s", e)
            return None

    if provider_name == "openrouter":
        try:
            return OpenRouterEmbeddingProvider(
                model=(getattr(cfg, "embedding_model", None)
                       or OpenRouterEmbeddingProvider.DEFAULT_MODEL),
                api_key_env=getattr(
                    cfg, "openrouter_api_key_env",
                    OpenRouterEmbeddingProvider.DEFAULT_API_KEY_ENV,
                ),
                base_url=getattr(cfg, "openrouter_base_url", None),
                dim_override=getattr(cfg, "embedding_dim", None),
            )
        except EmbeddingError as e:
            log.warning("OpenRouter embedding provider unavailable: %s", e)
            return None

    raise EmbeddingError(
        f"Unknown embedding_provider {provider_name!r}. "
        f"Supported: {', '.join(repr(p) for p in SUPPORTED_PROVIDERS)}."
    )
