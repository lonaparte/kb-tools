"""Unit tests for kb_core.paths — safe_resolve, to_relative,
is_book_chapter_filename.

These are the foundational path helpers that every other package
depends on. Break any of these silently and the whole toolchain
either rejects valid input or accepts unsafe input.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kb_core.paths import (
    PathError,
    safe_resolve,
    to_relative,
    is_book_chapter_filename,
    PAPERS_DIR, TOPICS_STANDALONE_DIR, TOPICS_AGENT_DIR, THOUGHTS_DIR,
    ACTIVE_SUBDIRS,
)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    """Fresh tmpdir KB with the 4 canonical subdirs laid out."""
    (tmp_path / "papers").mkdir()
    (tmp_path / "topics" / "standalone-note").mkdir(parents=True)
    (tmp_path / "topics" / "agent-created").mkdir(parents=True)
    (tmp_path / "thoughts").mkdir()
    return tmp_path


class TestSafeResolveAccepts:
    """Inputs safe_resolve must accept."""

    def test_simple_paper(self, kb):
        got = safe_resolve(kb, "papers/ABCD.md")
        assert got == (kb / "papers" / "ABCD.md").resolve()

    def test_no_md_suffix(self, kb):
        # safe_resolve doesn't validate the `.md` suffix — callers do.
        got = safe_resolve(kb, "papers/ABCD")
        assert got == (kb / "papers" / "ABCD").resolve()

    def test_nested_topic(self, kb):
        got = safe_resolve(kb, "topics/agent-created/stab/overview.md")
        assert (kb / "topics" / "agent-created" / "stab" / "overview.md").resolve() == got

    def test_backslash_normalised(self, kb):
        got = safe_resolve(kb, "papers\\ABCD.md")
        assert got == (kb / "papers" / "ABCD.md").resolve()

    def test_mixed_slash(self, kb):
        got = safe_resolve(kb, "topics\\agent-created/x.md")
        assert got == (kb / "topics" / "agent-created" / "x.md").resolve()


class TestSafeResolveRejects:
    """Inputs safe_resolve must reject — these are the safety
    boundaries. Regressing any of these is a security bug."""

    def test_empty(self, kb):
        with pytest.raises(PathError):
            safe_resolve(kb, "")

    def test_whitespace_only(self, kb):
        # Docstring promises "empty OR whitespace-only" → PathError.
        # Pre-0.28.2 the implementation was just `if not rel`, which
        # accepted spaces/tabs and tried to resolve them as a literal
        # filename. Regression test for the stress-run finding.
        for s in ("   ", "\t", "\n", "  \t \n "):
            with pytest.raises(PathError):
                safe_resolve(kb, s)

    def test_absolute_posix(self, kb):
        with pytest.raises(PathError):
            safe_resolve(kb, "/etc/passwd")

    def test_absolute_backslash(self, kb):
        with pytest.raises(PathError):
            safe_resolve(kb, "\\Windows\\System32")

    def test_drive_letter_forward(self, kb):
        with pytest.raises(PathError):
            safe_resolve(kb, "C:/Windows")

    def test_drive_letter_backward(self, kb):
        with pytest.raises(PathError):
            safe_resolve(kb, "D:\\data")

    def test_escape_dotdot(self, kb):
        with pytest.raises(PathError):
            safe_resolve(kb, "../outside")

    def test_escape_dotdot_midpath(self, kb):
        # The classic "papers/../../etc/passwd" — resolve() collapses
        # the .. first, and the result escapes kb_root.
        with pytest.raises(PathError):
            safe_resolve(kb, "papers/../../etc/passwd")

    def test_crafted_drive_via_backslash(self, kb):
        # A canary for the "reject drive letter BEFORE normalising
        # slashes" rule. Without the pre-normalise check a crafted
        # input like "\\C:\\Windows" could slip through.
        with pytest.raises(PathError):
            safe_resolve(kb, "\\C:\\Windows")


class TestToRelative:
    def test_round_trip(self, kb):
        abs_path = safe_resolve(kb, "papers/X.md")
        rel = to_relative(kb, abs_path)
        assert rel == "papers/X.md"

    def test_always_posix_style(self, kb):
        # Even on Windows, to_relative returns forward slashes —
        # the KB protocol is POSIX for cross-machine stability.
        abs_path = safe_resolve(kb, "topics/agent-created/x/y.md")
        rel = to_relative(kb, abs_path)
        assert "/" in rel
        assert "\\" not in rel


class TestIsBookChapterFilename:
    def test_recognised(self):
        assert is_book_chapter_filename("BOOKKEY-ch03.md") == ("BOOKKEY", 3)

    def test_single_digit_number(self):
        # Tolerates 1-digit, though the convention is 2+.
        assert is_book_chapter_filename("K-ch1.md") == ("K", 1)

    def test_multi_digit_number(self):
        assert is_book_chapter_filename("K-ch117.md") == ("K", 117)

    def test_whole_book_is_none(self):
        assert is_book_chapter_filename("BOOKKEY.md") is None

    def test_no_md_suffix_is_none(self):
        assert is_book_chapter_filename("BOOKKEY-ch03") is None

    def test_regular_paper_is_none(self):
        assert is_book_chapter_filename("ABCD1234.md") is None

    def test_hyphen_in_key_ok(self):
        # The key captured is the left-maximal match — keys may
        # contain hyphens, so "my-book-ch05.md" → ("my-book", 5).
        assert is_book_chapter_filename("my-book-ch05.md") == ("my-book", 5)


class TestLayoutConstants:
    def test_active_subdirs_in_order(self):
        assert ACTIVE_SUBDIRS == (
            PAPERS_DIR, TOPICS_STANDALONE_DIR,
            TOPICS_AGENT_DIR, THOUGHTS_DIR,
        )

    def test_topics_subdirs_are_two_seg(self):
        # v26 specifically — topics/ has two sub-buckets, not a
        # direct top-level scan.
        assert TOPICS_STANDALONE_DIR == "topics/standalone-note"
        assert TOPICS_AGENT_DIR == "topics/agent-created"
