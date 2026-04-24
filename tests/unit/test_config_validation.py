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

    def test_valid_log_levels(self):
        """Valid log levels should pass through normalized."""
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

    def test_invalid_log_level_raises(self):
        """Invalid log levels should raise ConfigError."""
        with pytest.raises(ConfigError) as exc:
            _validate_log_level("trace")
        assert "Invalid log_level" in str(exc.value)
        assert "trace" in str(exc.value)

        with pytest.raises(ConfigError) as exc:
            _validate_log_level("verbose")
        assert "Invalid log_level" in str(exc.value)

        with pytest.raises(ConfigError) as exc:
            _validate_log_level("fatal")
        assert "Invalid log_level" in str(exc.value)


class TestBatchSizeValidation:
    """Test batch size validation with provider limits."""

    def test_openai_within_limit(self):
        """OpenAI batch size within limit should pass through."""
        assert _validate_batch_size(100, "openai") == 100
        assert _validate_batch_size(2048, "openai") == 2048

    def test_openai_exceeds_limit(self):
        """OpenAI batch size exceeding 2048 should be capped."""
        assert _validate_batch_size(3000, "openai") == 2048
        assert _validate_batch_size(10000, "openai") == 2048

    def test_gemini_within_limit(self):
        """Gemini batch size within limit should pass through."""
        assert _validate_batch_size(50, "gemini") == 50
        assert _validate_batch_size(100, "gemini") == 100

    def test_gemini_exceeds_limit(self):
        """Gemini batch size exceeding 100 should be capped."""
        assert _validate_batch_size(200, "gemini") == 100
        assert _validate_batch_size(1000, "gemini") == 100

    def test_openrouter_within_limit(self):
        """OpenRouter batch size within limit should pass through."""
        assert _validate_batch_size(100, "openrouter") == 100
        assert _validate_batch_size(2048, "openrouter") == 2048

    def test_openrouter_exceeds_limit(self):
        """OpenRouter batch size exceeding 2048 should be capped."""
        assert _validate_batch_size(3000, "openrouter") == 2048

    def test_unknown_provider_uses_conservative_limit(self):
        """Unknown provider should use most conservative limit (100)."""
        assert _validate_batch_size(50, "unknown") == 50
        assert _validate_batch_size(150, "unknown") == 100
