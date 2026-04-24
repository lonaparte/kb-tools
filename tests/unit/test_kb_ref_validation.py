"""Tests for the v0.28.2 tightening of validate_kb_ref_entry.

Pre-0.28.2 these cases all leaked through (stress-run finding D):

  - papers/                      (empty tail)
  - topics/agent-created/        (empty tail)
  - topics/standalone-note/      (empty tail)
  - topics/agent-created/a/b/c   (multi-segment tail)
  - papers/BAD SLUG              (non-validated tail)
  - papers/lowercase             (not a Zotero key)
  - thoughts/not-date-prefixed   (not a canonical thought slug)

All of them now raise RuleViolation. Canonical shapes still accepted.
"""
from __future__ import annotations

import pytest


# ---- shapes that MUST be accepted ------------------------------------

VALID = [
    # Bare key — caller disambiguates.
    "papers",
    # Papers: 8-char Zotero key (whole work).
    "papers/ABCD1234",
    # Papers: chapter.
    "papers/ABCD1234-ch03",
    "papers/ABCD1234-ch123",
    # Topics agent-created: flat kebab.
    "topics/agent-created/foo",
    "topics/agent-created/foo-bar-baz",
    "topics/agent-created/a1b2",
    # Topics standalone-note: 8-char Zotero key.
    "topics/standalone-note/ABCD1234",
    # Thoughts: YYYY-MM-DD-kebab.
    "thoughts/2026-04-22-foo",
    "thoughts/2026-01-15-foo-bar",
]

# ---- shapes that MUST be rejected -----------------------------------

INVALID = [
    # Empty / whitespace.
    "",
    "   ",
    "\t",
    # Absolute / traversal.
    "/etc/passwd",
    "papers/../../etc",
    # Empty tails.
    "papers/",
    "thoughts/",
    "topics/agent-created/",
    "topics/standalone-note/",
    # Multi-segment tails.
    "papers/ABCD1234/extra",
    "thoughts/2026-04-24-foo/extra",
    "topics/agent-created/a/b",
    # Invalid papers key.
    "papers/BAD SLUG",       # space
    "papers/lowercase",       # not uppercase
    "papers/short",           # too short
    "papers/VERYLONGKEY123",  # too long (no chNN)
    # Invalid agent-created slug.
    "topics/agent-created/CAPS",
    "topics/agent-created/with space",
    "topics/agent-created/with_under",  # underscore
    # Invalid standalone-note key.
    "topics/standalone-note/lowercase",
    "topics/standalone-note/TOO-LONG-KEY",
    # Invalid thought slug.
    "thoughts/not-date-prefixed",
    "thoughts/2026-04-24-BadCase",
    # Unknown subdir.
    "foo/bar",
    "topics/nonexistent/slug",
    # Deprecated shapes (special pointed messages).
    "zotero-notes/ABCD1234",
    "topics/top-level-legacy",
]


@pytest.mark.parametrize("entry", VALID)
def test_accepts_valid(entry):
    from kb_write.rules import validate_kb_ref_entry
    # Should not raise.
    validate_kb_ref_entry(entry)


@pytest.mark.parametrize("entry", INVALID)
def test_rejects_invalid(entry):
    from kb_write.rules import validate_kb_ref_entry, RuleViolation
    with pytest.raises(RuleViolation):
        validate_kb_ref_entry(entry)


def test_non_string_rejected():
    from kb_write.rules import validate_kb_ref_entry, RuleViolation
    for bad in [None, 42, [], {}]:
        with pytest.raises(RuleViolation):
            validate_kb_ref_entry(bad)  # type: ignore[arg-type]
