"""Regression for the v0.27.10 write_lock re-entrancy bug AND
the v0.28.0 follow-up rewrite.

v0.27.10 discovery: same-process nested `write_lock` calls hit
the "PID matches, stale lock, take it over" branch, unlinked
the file, and a subsequent inner exit deleted the lock while
outer was still in critical section.

v0.28.0 rewrite: switched from O_EXCL+PID-file protocol to
fcntl.flock. Eliminates the empty-file window at acquisition
time (which was causing actual data loss at 100-way
concurrent tag writes — 95/100 tags landing instead of 100).
The lock file persists as an anchor; acquisition is signalled
by kernel flock state, not file existence.

These tests now check lock SEMANTICS (can another acquirer
proceed?) rather than file existence."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _peer_timeout_script(kb_root, lock_key_extra=""):
    """Script that tries to acquire the same lock with a short
    timeout; exits 0 if it got through (lock was NOT held), 1 if
    it timed out (lock IS held)."""
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, "/home/llm-agent/workspace/KB/kb-tools/kb_core/src")
        sys.path.insert(0, "/home/llm-agent/workspace/KB/kb-tools/kb_write/src")
        from pathlib import Path
        from kb_write.atomic import write_lock{lock_key_extra}
        try:
            with write_lock(Path(r"{kb_root}"), timeout=0.5):
                sys.exit(0)  # got through → lock was NOT held
        except TimeoutError:
            sys.exit(1)  # timed out → lock IS held
    """)


def _sibling_can_acquire(kb_root, *, timeout=0.5) -> bool:
    """Fork a peer process, try to acquire the same lock with a
    short timeout. Returns True if peer got through (we aren't
    holding), False if peer timed out (we are holding)."""
    script = _peer_timeout_script(str(kb_root))
    r = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def test_nested_write_lock_holds_through_inner_exit(tmp_path):
    """Outer write_lock holds; inner enters + exits; a sibling
    process trying to acquire during outer's critical section
    (after inner has exited) MUST time out — the outer lock is
    still held. Pre-0.27.10 bug: inner exit deleted the lock
    file, sibling would walk in."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock(kb):
        # Sibling can't acquire while we hold.
        assert not _sibling_can_acquire(kb), (
            "sibling acquired while outer held — lock is not "
            "providing mutual exclusion"
        )

        # Nested acquire inside same process (re-entrant).
        with write_lock(kb):
            # Still can't be acquired by sibling.
            assert not _sibling_can_acquire(kb)

        # Inner exited; outer still active. Sibling still can't.
        assert not _sibling_can_acquire(kb), (
            "inner exit released the lock while outer was still "
            "active — this is the pre-0.27.10 re-entrancy bug"
        )

    # Outer exited — sibling can now acquire.
    assert _sibling_can_acquire(kb)


def test_deeply_nested_write_lock(tmp_path):
    """Depth-3 nesting. All three exits must decrement before
    the lock is actually released."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock(kb):
        with write_lock(kb):
            with write_lock(kb):
                assert not _sibling_can_acquire(kb)
            assert not _sibling_can_acquire(kb), (
                "depth-3 inner exit released lock prematurely"
            )
        assert not _sibling_can_acquire(kb), (
            "depth-2 inner exit released lock prematurely"
        )

    assert _sibling_can_acquire(kb)


def test_sequential_acquire_release(tmp_path):
    """Not re-entrant — sequential acquires. Each lock/unlock
    cycle should leave the next acquire unblocked."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock(kb):
        assert not _sibling_can_acquire(kb)
    assert _sibling_can_acquire(kb)

    with write_lock(kb):
        assert not _sibling_can_acquire(kb)
    assert _sibling_can_acquire(kb)


def test_independent_kb_roots(tmp_path):
    """Lock on kb_a doesn't affect kb_b."""
    from kb_write.atomic import write_lock

    kb_a = tmp_path / "kb_a"
    kb_b = tmp_path / "kb_b"
    (kb_a / ".kb-mcp").mkdir(parents=True)
    (kb_b / ".kb-mcp").mkdir(parents=True)

    with write_lock(kb_a):
        # kb_a contended, kb_b free.
        assert not _sibling_can_acquire(kb_a)
        assert _sibling_can_acquire(kb_b)

        with write_lock(kb_b):
            # Both held by us.
            assert not _sibling_can_acquire(kb_a)
            assert not _sibling_can_acquire(kb_b)

        # kb_b released; kb_a still held.
        assert not _sibling_can_acquire(kb_a)
        assert _sibling_can_acquire(kb_b)

    assert _sibling_can_acquire(kb_a)
    assert _sibling_can_acquire(kb_b)


def test_exception_inside_inner_releases_counter(tmp_path):
    """If an exception propagates out of an inner write_lock, the
    outer lock must still be intact (depth counter must
    decrement via finally)."""
    from kb_write.atomic import write_lock

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock(kb):
        assert not _sibling_can_acquire(kb)
        try:
            with write_lock(kb):
                raise RuntimeError("synthetic failure in inner")
        except RuntimeError:
            pass
        # Outer still held.
        assert not _sibling_can_acquire(kb)

    assert _sibling_can_acquire(kb)
