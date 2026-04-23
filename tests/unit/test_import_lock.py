"""Tests for kb_importer.import_lock — cross-process mutex."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


# Windows has no fcntl → lock is a no-op. Skip this whole suite
# rather than pretend to test exclusion that can't be enforced.
pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="fcntl-based lock is no-op on Windows",
)


from kb_importer.import_lock import (
    import_lock,
    ImportLockHeld,
    IMPORT_LOCK_REL,
)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / ".kb-mcp").mkdir()
    return tmp_path


class TestLockAcquire:
    def test_first_acquire_succeeds(self, kb):
        with import_lock(kb):
            pass  # acquired + released cleanly

    def test_lock_file_written_during_hold(self, kb):
        lock_path = kb / IMPORT_LOCK_REL
        with import_lock(kb):
            assert lock_path.exists(), "lock file must exist while held"
            # Content should be JSON with pid + started_at.
            import json
            data = json.loads(lock_path.read_text())
            assert data["pid"] == os.getpid()
            assert "started_at" in data

    def test_lock_file_cleaned_after_release(self, kb):
        lock_path = kb / IMPORT_LOCK_REL
        with import_lock(kb):
            pass
        # Best-effort unlink — file should be gone.
        assert not lock_path.exists()


class TestLockContention:
    def test_second_acquire_raises(self, kb):
        """Two import_lock() in the same process still serialise
        via fcntl — the second attempt while the first is still
        active must raise ImportLockHeld."""
        with import_lock(kb):
            with pytest.raises(ImportLockHeld):
                with import_lock(kb):
                    pytest.fail("inner lock should not acquire")

    def test_after_release_reusable(self, kb):
        with import_lock(kb):
            pass
        # Must acquire again cleanly.
        with import_lock(kb):
            pass

    def test_error_message_mentions_pid(self, kb):
        with import_lock(kb):
            try:
                with import_lock(kb):
                    pytest.fail("should not acquire")
            except ImportLockHeld as e:
                msg = str(e)
                assert str(os.getpid()) in msg, (
                    f"error message should include holder pid for "
                    f"troubleshooting: {msg!r}"
                )
                assert e.holder_pid == os.getpid()


class TestStaleFile:
    def test_stale_lock_file_without_held_lock_is_reusable(self, kb):
        """If a prior run crashed, the file may exist but flock
        will succeed — we should overwrite and proceed."""
        (kb / ".kb-mcp").mkdir(exist_ok=True)
        stale = kb / IMPORT_LOCK_REL
        stale.write_text('{"pid": 999999, "started_at": "ancient"}\n')
        # Fresh acquire should succeed (OS doesn't hold the flock
        # for the dead pid's file).
        with import_lock(kb):
            import json
            data = json.loads(stale.read_text())
            # Our pid, not the stale 999999.
            assert data["pid"] == os.getpid()
