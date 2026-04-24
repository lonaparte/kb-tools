"""Tests for the 1.4.0 circuit breaker helper in import_fulltext.

The helper is `_CircuitBreaker` — a sliding window over the last N
error codes that trips when the window fills up with the same
breaker-relevant code. Success resets the window.

The full integration (batch loop + fallback_state interaction) is
covered by e2e; these unit tests lock in the helper's invariants.
"""
from __future__ import annotations

from kb_importer.commands.import_fulltext import _CircuitBreaker


_CODES = {"llm_bad_request", "llm_other", "other"}


def test_disabled_never_trips():
    b = _CircuitBreaker(window=0, relevant_codes=_CODES)
    assert not b.enabled
    for _ in range(100):
        assert b.record_failure("llm_other") is False


def test_trips_after_n_consecutive_same_code():
    b = _CircuitBreaker(window=3, relevant_codes=_CODES)
    assert b.record_failure("llm_other") is False
    assert b.record_failure("llm_other") is False
    assert b.record_failure("llm_other") is True  # trips
    assert b.tripped_on == "llm_other"


def test_does_not_trip_with_mixed_codes():
    """Window of 3 with mixed codes — shouldn't trip. Streak means
    SAME code, not just any 3 failures."""
    b = _CircuitBreaker(window=3, relevant_codes=_CODES)
    b.record_failure("llm_other")
    b.record_failure("llm_bad_request")
    assert b.record_failure("llm_other") is False
    assert b.tripped_on is None


def test_success_clears_streak():
    """A successful paper between failures resets the counter."""
    b = _CircuitBreaker(window=3, relevant_codes=_CODES)
    b.record_failure("llm_other")
    b.record_failure("llm_other")
    b.record_success()
    assert b.record_failure("llm_other") is False
    assert b.record_failure("llm_other") is False
    # Three-in-a-row after the success: now trips.
    assert b.record_failure("llm_other") is True


def test_non_relevant_code_does_not_enter_window():
    """pdf_missing / already_processed etc. are per-paper local
    issues, not provider-health signals. They must not fill the
    breaker window."""
    b = _CircuitBreaker(window=3, relevant_codes=_CODES)
    b.record_failure("pdf_missing")
    b.record_failure("pdf_unreadable")
    b.record_failure("already_processed")
    assert b.tripped_on is None
    # Now add 3 real breaker-relevant failures — trips exactly at 3.
    b.record_failure("llm_other")
    b.record_failure("llm_other")
    assert b.record_failure("llm_other") is True


def test_window_of_one_trips_immediately():
    b = _CircuitBreaker(window=1, relevant_codes=_CODES)
    assert b.record_failure("llm_other") is True


def test_tripped_state_is_sticky():
    """Once tripped, subsequent records don't change tripped_on."""
    b = _CircuitBreaker(window=2, relevant_codes=_CODES)
    b.record_failure("llm_other")
    b.record_failure("llm_other")
    assert b.tripped_on == "llm_other"
    # Additional failures don't change or clear it.
    b.record_failure("llm_bad_request")
    assert b.tripped_on == "llm_other"
