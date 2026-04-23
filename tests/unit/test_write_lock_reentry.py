"""Regression for the v0.27.10 write_lock re-entrancy bug.

Pre-0.27.10, when the same process called `write_lock()` while
already holding it, the O_EXCL create would fail (file exists),
the code would read the PID from the lock file, see it matched
the current process, and fall through to the "Stale lock — take
it over" branch. That unlinked the lock file and created a fresh
one. When the inner scope exited it would unlink the lock while
the outer scope was still in its critical section, letting a
sibling process walk in.

v0.27.10 adds an in-process re-entry counter: nested acquisitions
share the on-disk lock file; only the outermost exit unlinks."""
from __future__ import annotations

import os


def test_nested_write_lock_preserves_outer_lock(tmp_path):
    """Outer write_lock holds; inner enters + exits; outer
    critical section must still see the lock file present."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()
    lock_path = kb / ".kb-mcp" / "write.lock"

    assert not lock_path.exists()
    with write_lock(kb):
        assert lock_path.exists(), "outer acquire should have created lock"
        assert lock_path.read_text().strip() == str(os.getpid())

        # Nested acquire.
        with write_lock(kb):
            # Lock file still present, still owned by us.
            assert lock_path.exists()
            assert lock_path.read_text().strip() == str(os.getpid())

        # Inner exited — outer is still active; lock MUST remain.
        assert lock_path.exists(), (
            "inner exit deleted the lock while outer was still "
            "in its critical section — this is the pre-0.27.10 "
            "re-entrancy bug"
        )
        assert lock_path.read_text().strip() == str(os.getpid())

    # Outer exited — NOW the lock should be gone.
    assert not lock_path.exists()


def test_deeply_nested_write_lock(tmp_path):
    """Depth-3 nesting. Only the outermost exit unlinks."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()
    lock_path = kb / ".kb-mcp" / "write.lock"

    with write_lock(kb):
        assert lock_path.exists()
        with write_lock(kb):
            assert lock_path.exists()
            with write_lock(kb):
                assert lock_path.exists()
            assert lock_path.exists(), "depth-3 inner exit killed lock"
        assert lock_path.exists(), "depth-2 inner exit killed lock"
    assert not lock_path.exists()


def test_sequential_acquire_release_still_works(tmp_path):
    """Not re-entrant — just two sequential lock acquisitions.
    Each should create-then-unlink normally."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()
    lock_path = kb / ".kb-mcp" / "write.lock"

    with write_lock(kb):
        assert lock_path.exists()
    assert not lock_path.exists()

    with write_lock(kb):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_reentry_tracker_not_leaked_across_different_kb_roots(tmp_path):
    """Two different kb_roots have independent lock files;
    acquiring one should not interfere with the other."""
    from kb_write.atomic import write_lock

    kb_a = tmp_path / "kb_a"
    kb_b = tmp_path / "kb_b"
    (kb_a / ".kb-mcp").mkdir(parents=True)
    (kb_b / ".kb-mcp").mkdir(parents=True)

    with write_lock(kb_a):
        assert (kb_a / ".kb-mcp" / "write.lock").exists()
        assert not (kb_b / ".kb-mcp" / "write.lock").exists()

        # Acquire B — a fresh, non-reentrant acquire.
        with write_lock(kb_b):
            assert (kb_a / ".kb-mcp" / "write.lock").exists()
            assert (kb_b / ".kb-mcp" / "write.lock").exists()

        # B released; A still held.
        assert (kb_a / ".kb-mcp" / "write.lock").exists()
        assert not (kb_b / ".kb-mcp" / "write.lock").exists()

    assert not (kb_a / ".kb-mcp" / "write.lock").exists()
    assert not (kb_b / ".kb-mcp" / "write.lock").exists()


def test_exception_inside_inner_does_not_strand_outer_lock(tmp_path):
    """If an exception propagates out of an inner write_lock,
    the outer lock must still be intact (depth counter must
    decrement via finally)."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()
    lock_path = kb / ".kb-mcp" / "write.lock"

    with write_lock(kb):
        assert lock_path.exists()
        try:
            with write_lock(kb):
                assert lock_path.exists()
                raise RuntimeError("synthetic failure in inner")
        except RuntimeError:
            pass
        # Outer still active, lock still held.
        assert lock_path.exists()

    assert not lock_path.exists()
