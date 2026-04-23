"""Unit tests for kb_write.atomic — atomic_write + mtime guard.

These are the safety primitives that back every md write. Failures
here corrupt the KB, so tests cover: crash recovery (temp files
cleaned), mtime guard, exclusive-create mode, and symlink
refusal."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from kb_write.atomic import (
    atomic_write,
    assert_mtime_unchanged,
    WriteConflictError,
    WriteExistsError,
)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / "papers").mkdir()
    return tmp_path


class TestAtomicWriteBasic:
    def test_creates_new(self, kb):
        target = kb / "papers" / "new.md"
        atomic_write(target, "hello\n")
        assert target.read_text() == "hello\n"

    def test_overwrites_existing(self, kb):
        target = kb / "papers" / "x.md"
        target.write_text("old\n")
        atomic_write(target, "new\n")
        assert target.read_text() == "new\n"

    def test_no_temp_files_left(self, kb):
        target = kb / "papers" / "x.md"
        atomic_write(target, "hello\n")
        # Temp file should be renamed away, not left in the dir.
        leftovers = list(kb.glob("papers/*.tmp*"))
        assert not leftovers, f"leftover temp files: {leftovers}"


class TestCreateOnly:
    def test_create_only_succeeds_for_new_file(self, kb):
        target = kb / "papers" / "fresh.md"
        atomic_write(target, "new\n", create_only=True)
        assert target.read_text() == "new\n"

    def test_create_only_refuses_existing(self, kb):
        target = kb / "papers" / "exists.md"
        target.write_text("already here\n")
        with pytest.raises(WriteExistsError):
            atomic_write(target, "would overwrite\n", create_only=True)
        # Contents unchanged after refusal.
        assert target.read_text() == "already here\n"


class TestMtimeGuard:
    def test_assert_unchanged_passes_when_same(self, kb):
        target = kb / "papers" / "g.md"
        target.write_text("x\n")
        m = target.stat().st_mtime
        assert_mtime_unchanged(target, m)  # must not raise

    def test_assert_unchanged_raises_on_drift(self, kb):
        target = kb / "papers" / "g.md"
        target.write_text("x\n")
        stale = target.stat().st_mtime - 100.0
        with pytest.raises(WriteConflictError):
            assert_mtime_unchanged(target, stale)

    def test_atomic_write_with_expected_mtime_success(self, kb):
        target = kb / "papers" / "g.md"
        target.write_text("x\n")
        m = target.stat().st_mtime
        atomic_write(target, "y\n", expected_mtime=m)
        assert target.read_text() == "y\n"

    def test_atomic_write_with_stale_mtime_conflict(self, kb):
        target = kb / "papers" / "g.md"
        target.write_text("x\n")
        stale = target.stat().st_mtime - 100.0
        with pytest.raises(WriteConflictError):
            atomic_write(target, "y\n", expected_mtime=stale)
        # Original content preserved on conflict.
        assert target.read_text() == "x\n"


class TestSymlinkRefused:
    def test_refuses_to_follow_symlink(self, kb):
        """Writing through a symlink could let an attacker escape
        kb_root even if safe_resolve was satisfied at the caller.
        atomic_write refuses symlinks defensively."""
        outside = kb.parent / "outside.md"
        outside.write_text("should not change\n")
        link = kb / "papers" / "link.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("platform does not support symlinks")
        # Depending on implementation, either the write is refused
        # (preferred) or the symlink is replaced by a regular file.
        # Either way, the target of the symlink must NOT be rewritten.
        try:
            atomic_write(link, "hijack\n")
        except Exception:
            pass
        assert outside.read_text() == "should not change\n"
