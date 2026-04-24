"""Unit tests for config validation (robustness improvements)."""
from __future__ import annotations

import pytest

from kb_mcp.config import (
    ConfigError,
    _validate_log_level,
    _validate_batch_size,
)


class TestLogLevelValidation:
    """Test log level validation."""

    def test_canonical_log_levels(self):
        """The five canonical levels should pass through normalized."""
        assert _validate_log_level("debug") == "debug"
        assert _validate_log_level("info") == "info"
        assert _validate_log_level("warning") == "warning"
        assert _validate_log_level("error") == "error"
        assert _validate_log_level("critical") == "critical"

    def test_case_insensitive(self):
        """Log levels should be case-insensitive."""
        assert _validate_log_level("DEBUG") == "debug"
        assert _validate_log_level("Info") == "info"
        assert _validate_log_level("WARNING") == "warning"
        assert _validate_log_level("Error") == "error"
        assert _validate_log_level("CRITICAL") == "critical"

    def test_whitespace_stripped(self):
        """Whitespace should be stripped."""
        assert _validate_log_level("  info  ") == "info"
        assert _validate_log_level("\tdebug\n") == "debug"

    def test_python_aliases_accepted_and_normalized(self):
        """Python's `logging` module defines `FATAL` as an alias for
        CRITICAL and `WARN` as an alias for WARNING. Rejecting them
        would be a regression — pre-validation, a user with
        `logging.level: fatal` got logging.CRITICAL correctly. Keep
        the alias accepted; normalise to the canonical name so
        downstream code only sees the five canonical strings.
        """
        assert _validate_log_level("fatal") == "critical"
        assert _validate_log_level("FATAL") == "critical"
        assert _validate_log_level("Fatal") == "critical"
        assert _validate_log_level("warn") == "warning"
        assert _validate_log_level("WARN") == "warning"

    def test_invalid_log_level_raises(self):
        """Truly unknown levels should raise ConfigError."""
        with pytest.raises(ConfigError) as exc:
            _validate_log_level("trace")
        assert "Invalid log_level" in str(exc.value)
        assert "trace" in str(exc.value)

        with pytest.raises(ConfigError) as exc:
            _validate_log_level("verbose")
        assert "Invalid log_level" in str(exc.value)

        with pytest.raises(ConfigError) as exc:
            _validate_log_level("nope")
        assert "Invalid log_level" in str(exc.value)


class TestBatchSizeValidation:
    """Test batch size validation with provider limits.

    Known providers get HARD errors for over-limit values (silent
    capping would surprise the user at runtime). Unknown providers
    get PASSED THROUGH unchanged — a user with a self-hosted OpenAI-
    compatible gateway (Ollama, vLLM, DashScope via openai_base_url)
    knows their own endpoint's limits better than we do.
    """

    def test_openai_within_limit(self):
        assert _validate_batch_size(100, "openai") == 100
        assert _validate_batch_size(2048, "openai") == 2048

    def test_openai_exceeds_limit_raises(self):
        """Over-limit for a known provider → ConfigError. Surfacing at
        load time beats silent capping (user-set batch_size=5000 would
        silently run at 2048; they'd wonder why indexing is slower
        than expected and never look at logs)."""
        with pytest.raises(ConfigError) as exc:
            _validate_batch_size(3000, "openai")
        msg = str(exc.value)
        assert "3000" in msg and "2048" in msg
        assert "openai" in msg

    def test_gemini_within_limit(self):
        assert _validate_batch_size(50, "gemini") == 50
        assert _validate_batch_size(100, "gemini") == 100

    def test_gemini_exceeds_limit_raises(self):
        with pytest.raises(ConfigError) as exc:
            _validate_batch_size(200, "gemini")
        msg = str(exc.value)
        assert "200" in msg and "100" in msg
        assert "gemini" in msg

    def test_openrouter_within_limit(self):
        assert _validate_batch_size(100, "openrouter") == 100
        assert _validate_batch_size(2048, "openrouter") == 2048

    def test_openrouter_exceeds_limit_raises(self):
        with pytest.raises(ConfigError) as exc:
            _validate_batch_size(3000, "openrouter")
        assert "openrouter" in str(exc.value)

    def test_unknown_provider_passes_through_unchanged(self):
        """Custom OpenAI-compatible endpoints (Ollama / vLLM / etc.)
        are reached via `openai_base_url`. Their batch limit depends
        on the local server config. We can't know it; silently
        capping to 100 would break the documented self-host use case.
        Trust the user."""
        assert _validate_batch_size(50, "unknown") == 50
        assert _validate_batch_size(150, "unknown") == 150
        assert _validate_batch_size(5000, "unknown") == 5000


class TestEmbeddingDimStrictParsing:
    """`embeddings.dim` goes through `_parse_positive_int` in
    load_config, matching the strictness of `batch_size`. Pre-fix,
    it was a bare `emb_cfg.get("dim")` — bool / float / negative /
    string values slipped through and failed later at INSERT time
    with a confusing sqlite-vec error. These tests verify that a
    dim typo is caught at config load.
    """

    def test_valid_positive_int_accepted(self):
        """The only valid shape for dim is a positive integer."""
        from kb_mcp.config import _parse_positive_int
        assert _parse_positive_int(1536, field="embeddings.dim") == 1536
        assert _parse_positive_int(768, field="embeddings.dim") == 768

    def test_zero_rejected(self):
        from kb_mcp.config import _parse_positive_int
        with pytest.raises(ConfigError) as exc:
            _parse_positive_int(0, field="embeddings.dim")
        assert "positive integer" in str(exc.value)

    def test_negative_rejected(self):
        from kb_mcp.config import _parse_positive_int
        with pytest.raises(ConfigError) as exc:
            _parse_positive_int(-1, field="embeddings.dim")
        assert "positive integer" in str(exc.value)

    def test_bool_rejected(self):
        """YAML `true`/`false` parses to Python bool, which is an
        int subclass. Silently accepting `dim: true` as 1 would be
        a confusing footgun."""
        from kb_mcp.config import _parse_positive_int
        with pytest.raises(ConfigError) as exc:
            _parse_positive_int(True, field="embeddings.dim")
        assert "bool" in str(exc.value)

    def test_float_rejected(self):
        """YAML `1536.0` parses to float. int(1536.0) == 1536 but
        int(1536.5) silently truncates to 1536 — reject outright."""
        from kb_mcp.config import _parse_positive_int
        with pytest.raises(ConfigError) as exc:
            _parse_positive_int(1536.0, field="embeddings.dim")
        assert "float" in str(exc.value)

    def test_bad_string_rejected(self):
        from kb_mcp.config import _parse_positive_int
        with pytest.raises(ConfigError) as exc:
            _parse_positive_int("not-an-int", field="embeddings.dim")
        assert "not an integer" in str(exc.value)
