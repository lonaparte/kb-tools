"""Atomic file writes with mtime conflict detection and an advisory
lock on the KB for belt-and-braces safety.

The write protocol (see AGENT-WRITE-RULES.md §4):

1. Caller stats target, records mtime.
2. Caller composes new content.
3. Caller calls `atomic_write(target, content, expected_mtime)`:
   a. Re-stats target; if mtime differs from expected, raise.
   b. Writes content to a temp file in the same directory.
   c. `os.replace(temp, target)` — atomic rename.

An advisory lock at `<kb_root>/.kb-mcp/write.lock` prevents two
`kb-write` processes from interleaving operations. It's process-level,
not file-level — coarse but simple.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WriteConflictError(Exception):
    """Raised when the target file changed between read and write.

    The caller's `expected_mtime` didn't match the current mtime,
    meaning another process (or editor) modified the file. Message
    includes both mtimes so the caller can show a useful diff.
    """

    def __init__(self, path: Path, expected: float, actual: float):
        super().__init__(
            f"{path} was modified between read and write "
            f"(expected mtime {expected:.9f}, got {actual:.9f}). "
            "Re-read the file and retry."
        )
        self.path = path
        self.expected = expected
        self.actual = actual


class WriteExistsError(Exception):
    """Raised when a create_* operation finds the target already
    exists. Callers should either overwrite explicitly or choose
    a new slug."""


def assert_mtime_unchanged(target: Path, expected_mtime: float) -> None:
    """Raise WriteConflictError if target's current mtime differs
    from `expected_mtime`. Used both by atomic_write (as a pre-write
    guard) and by no-op write paths that still want to surface
    concurrent-modification conflicts.

    Convention: if target doesn't exist, treat as "mtime = 0.0" —
    matches atomic_write's original behaviour.
    """
    if not target.exists():
        raise WriteConflictError(target, expected_mtime, 0.0)
    current = target.stat().st_mtime
    if abs(current - expected_mtime) > 1e-6:
        raise WriteConflictError(target, expected_mtime, current)


def atomic_write(
    target: Path,
    content: str,
    *,
    expected_mtime: float | None = None,
    create_only: bool = False,
) -> None:
    """Write `content` to `target` atomically.

    Args:
        target: absolute path to the destination file.
        content: full file contents (str; will be encoded utf-8).
        expected_mtime: if provided, compare against target's current
            mtime right before the rename. If mismatched, raise
            WriteConflictError. If the target doesn't exist yet,
            expected_mtime=None is the only safe value; non-None
            means "I expected this mtime" — if target is absent,
            that's a conflict too.
        create_only: if True, refuse to write when target exists.
            Used by create_thought/create_topic.

    On any failure after the temp file is written, the temp file is
    cleaned up. The target is either fully old or fully new — never
    half-written.
    """
    target = Path(target)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    # create_only: claim the target name atomically via O_EXCL BEFORE
    # doing anything else. A previous "check exists, then replace"
    # pattern had a TOCTOU race — two concurrent `create_thought`
    # calls could both pass the exists check and the later one would
    # silently clobber the earlier one via os.replace. O_EXCL makes
    # the name reservation atomic at the OS level.
    #
    # Side effect to clean up: this creates a 0-byte sentinel at the
    # target path. If subsequent steps fail (disk full, interrupted
    # write, mtime check conflict, etc.) we MUST delete the sentinel,
    # otherwise the failed creation "sticks" and a retry sees the
    # phantom and bounces off with WriteExistsError. Tracked via the
    # `created_sentinel` flag so the except path knows to undo it;
    # replace_succeeded is set to True after the final os.replace so
    # the flag doesn't trigger cleanup of the legitimate final file.
    created_sentinel = False
    replace_succeeded = False
    if create_only:
        try:
            excl_fd = os.open(
                str(target),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            raise WriteExistsError(
                f"{target} already exists; refusing to overwrite."
            )
        os.close(excl_fd)
        created_sentinel = True

    # mtime guard (only meaningful for update paths). In create_only
    # mode expected_mtime must be None — skip.
    if not create_only and expected_mtime is not None:
        assert_mtime_unchanged(target, expected_mtime)

    # Write to temp file in the SAME directory (so os.replace is a
    # rename, not a cross-fs copy).
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix="." + target.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Final mtime check before rename — as close to the atomic
        # action as possible.
        if expected_mtime is not None and target.exists():
            current = target.stat().st_mtime
            if abs(current - expected_mtime) > 1e-6:
                raise WriteConflictError(target, expected_mtime, current)
        os.replace(tmp_path, target)
        replace_succeeded = True
        # Also fsync the parent directory so the rename survives
        # power loss. Without this, the file's data is on disk but
        # the directory entry pointing at it might not be, and a
        # post-crash fsck could either roll back the rename or leave
        # the target in an undefined state. Best-effort: some
        # platforms (notably Windows) don't support O_RDONLY on a
        # directory and will raise here; we ignore that since those
        # filesystems don't expose directory fsync semantics anyway.
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
        except (OSError, PermissionError):
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except Exception:
        # Clean up temp on failure.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        # Clean up the O_EXCL sentinel if we created it and haven't
        # yet replaced it with real content. Without this, retrying
        # the failed operation hits the sentinel and returns
        # WriteExistsError — a false positive that blocks recovery.
        if created_sentinel and not replace_succeeded:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
        raise


# v0.27.10: in-process re-entry tracker.
#
# Pre-0.27.10 `write_lock` had a re-entrancy bug: when the same
# process tried to acquire the lock while already holding it, the
# O_EXCL create would fail (file exists), the code would read the
# PID from the existing file, see it matches the current PID, and
# fall through to the "Stale lock — take it over" branch. That
# unlinked the lock file and created a fresh one. When the inner
# scope exited it would unlink the lock — while the outer scope
# was still in its critical section — and a sibling process could
# then walk in.
#
# Fix: track lock holders in a per-process dict keyed by absolute
# lock-file path. On entry, if this process already owns the path,
# bump the depth and yield without touching the file. On exit,
# decrement; only unlink at depth 0. The on-disk PID file is
# still the cross-process signal; it just doesn't need
# re-acquiring from within the same process.
_held_locks: dict[str, int] = {}


@contextmanager
def write_lock(kb_root: Path, timeout: float = 10.0) -> Iterator[Path]:
    """Acquire an advisory write lock for the whole KB.

    Implementation: a PID file at `<kb_root>/.kb-mcp/write.lock`. If
    the file exists AND contains a live PID, another writer is
    active — we wait up to `timeout` seconds polling for it to clear.

    Stale locks (from crashed processes) are detected by checking
    whether the PID is running, and taken over.

    Re-entrant within a single process: nested `with write_lock(...)`
    calls share the on-disk lock file (inner acquisitions bump an
    in-memory depth counter; only the outermost exit unlinks).

    Not a kernel-level lock (would need `fcntl` on POSIX, `msvcrt` on
    Windows); this is good enough for the single-user case and
    correctly raises when lock is contended.
    """
    lock_dir = kb_root / ".kb-mcp"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "write.lock"
    lock_key = str(lock_path.resolve())
    my_pid = os.getpid()

    # Re-entrancy check: if this process already holds this lock
    # path, bump the depth and yield without touching the file.
    # Inner exits will decrement; the outermost exit unlinks.
    if _held_locks.get(lock_key, 0) > 0:
        _held_locks[lock_key] += 1
        try:
            yield lock_path
        finally:
            _held_locks[lock_key] -= 1
            # Never unlink from a nested exit — outer still holds.
        return

    deadline = time.monotonic() + timeout
    while True:
        try:
            # O_EXCL ensures creation is atomic.
            fd = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
            with os.fdopen(fd, "w") as f:
                f.write(str(my_pid))
            break
        except FileExistsError:
            # Someone else holds it. Check if their PID is alive.
            try:
                other_pid = int(lock_path.read_text().strip())
                if other_pid != my_pid and _pid_alive(other_pid):
                    # Real contention. Wait.
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"write lock held by pid {other_pid} "
                            f"({lock_path}); giving up after {timeout}s."
                        )
                    time.sleep(0.1)
                    continue
                # Stale lock. Either (a) PID dead or (b) PID same as
                # ours but _held_locks says we're NOT already
                # holding it — means the previous process (possibly
                # an ancestor with PID reuse, or an earlier crash
                # that left state) abandoned it. Take it over.
                lock_path.unlink(missing_ok=True)
                continue
            except (ValueError, FileNotFoundError):
                # Lock file corrupted or gone; retry.
                lock_path.unlink(missing_ok=True)
                continue

    _held_locks[lock_key] = 1
    try:
        yield lock_path
    finally:
        _held_locks[lock_key] -= 1
        if _held_locks[lock_key] <= 0:
            _held_locks.pop(lock_key, None)
            # Only remove the on-disk file if it's still ours
            # (defensive: something else might have taken over if
            # we hung for a long time).
            try:
                pid_in_file = int(lock_path.read_text().strip())
                if pid_in_file == my_pid:
                    lock_path.unlink(missing_ok=True)
            except Exception:
                pass


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a PID is a running process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, 0, pid
            )
            if h == 0:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but we can't signal it.
        return True
