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
import re
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


# v0.28.0: switch locking primitive from PID-file-with-O_EXCL to
# POSIX fcntl.flock. Pre-0.28.0 used O_EXCL to create a PID file
# and then wrote the PID in a second step. That left a window
# where the file existed-but-was-empty, and a sibling process
# that hit that window would: FileExistsError → read empty text
# → int("") raises ValueError → fall into the "corrupted lock,
# take over" branch → unlink the fresh lock the first process
# just created → O_EXCL succeeds on the next loop → both
# processes now think they hold the lock. Observed in the field
# at 100-way concurrent `kb-write tag add` on the same paper:
# 95/100 tags landed, 5 lost silently.
#
# fcntl.flock eliminates the window entirely — lock acquisition
# and PID-file existence are decoupled. We keep the file around
# as a long-lived anchor (at `.kb-mcp/write.lock`) and use
# fcntl to atomically acquire/release exclusive access. Writing
# the PID is just for diagnostics; it no longer participates in
# the locking protocol.
#
# In-process re-entrancy is still tracked in `_held_locks` so
# nested `with write_lock(...)` within the same process shares
# the single on-disk acquisition (flock is per-(process,fd),
# and acquiring twice on the same fd within one process is
# idempotent on Linux but does not track depth — so we track it
# ourselves for clean symmetric decrement on exit).
#
# Windows falls back to the pre-0.28.0 shape since msvcrt.locking
# doesn't match fcntl's semantics. The race was Linux-observed;
# Windows correctness hasn't been field-tested recently, and
# adding a Windows fcntl equivalent is out of scope.
_held_locks: dict[tuple[int, str], int] = {}
_held_lock_fds: dict[tuple[int, str], int] = {}  # (tid, key) → fd

def _lock_tid() -> int:
    """Per-thread identity for the re-entrancy tracker. Two
    threads in the same process must NOT share the re-entrant
    depth — each thread needs its own fcntl acquisition or one
    thread's critical section leaks into the other's."""
    import threading
    return threading.get_ident()


@contextmanager
def write_lock(kb_root: Path, timeout: float = 10.0) -> Iterator[Path]:
    """Acquire an advisory write lock for the whole KB.

    Implementation on POSIX: open+fcntl.flock(LOCK_EX) on
    `<kb_root>/.kb-mcp/write.lock`. The kernel serialises
    exclusive acquisitions atomically — no read-empty-file race.
    The on-disk lock file persists across acquisitions as a
    stable anchor; we don't unlink it.

    Re-entrant within a single process: nested `with write_lock(...)`
    calls share the flock via an in-process depth counter; only
    the outermost exit releases the kernel lock.

    Windows: falls back to the pre-0.28.0 PID-file dance. The race
    is Linux-observed; Windows correctness is accepted with the
    older shape until someone hits the wall.
    """
    lock_dir = kb_root / ".kb-mcp"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "write.lock"
    lock_key = str(lock_path.resolve())

    # Re-entrancy check: same process already holds this path.
    if _held_locks.get((_lock_tid(), lock_key), 0) > 0:
        _held_locks[(_lock_tid(), lock_key)] += 1
        try:
            yield lock_path
        finally:
            _held_locks[(_lock_tid(), lock_key)] -= 1
        return

    if sys.platform == "win32":
        yield from _write_lock_win_legacy(lock_path, lock_key, timeout)
        return

    # POSIX: fcntl.flock path.
    import fcntl
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    # Read peer's PID for the error message (best
                    # effort — file may be empty during the peer's
                    # own PID-write step, but that window is tiny
                    # and non-load-bearing).
                    try:
                        other = os.pread(fd, 64, 0).decode().strip() or "?"
                    except Exception:
                        other = "?"
                    raise TimeoutError(
                        f"write lock held by pid {other} "
                        f"({lock_path}); giving up after {timeout}s."
                    )
                time.sleep(0.05)
        # Write our PID for diagnostics. Not part of the locking
        # protocol — the flock IS the lock.
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
    except BaseException:
        os.close(fd)
        raise

    _held_locks[(_lock_tid(), lock_key)] = 1
    _held_lock_fds[(_lock_tid(), lock_key)] = fd
    try:
        yield lock_path
    finally:
        _held_locks[(_lock_tid(), lock_key)] -= 1
        if _held_locks[(_lock_tid(), lock_key)] <= 0:
            _held_locks.pop((_lock_tid(), lock_key), None)
            stored_fd = _held_lock_fds.pop((_lock_tid(), lock_key), fd)
            try:
                fcntl.flock(stored_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(stored_fd)


def _write_lock_win_legacy(
    lock_path: Path, lock_key: str, timeout: float,
):
    """Pre-0.28.0 PID-file-with-O_EXCL path, retained for Windows
    where fcntl isn't available. Has the empty-file race; only
    reach here on `win32`."""
    my_pid = os.getpid()
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
            with os.fdopen(fd, "w") as f:
                f.write(str(my_pid))
            break
        except FileExistsError:
            try:
                other_pid = int(lock_path.read_text().strip())
                if other_pid != my_pid and _pid_alive(other_pid):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"write lock held by pid {other_pid} "
                            f"({lock_path}); giving up after {timeout}s."
                        )
                    time.sleep(0.1)
                    continue
                lock_path.unlink(missing_ok=True)
                continue
            except (ValueError, FileNotFoundError):
                lock_path.unlink(missing_ok=True)
                continue

    _held_locks[(_lock_tid(), lock_key)] = 1
    try:
        yield lock_path
    finally:
        _held_locks[(_lock_tid(), lock_key)] -= 1
        if _held_locks[(_lock_tid(), lock_key)] <= 0:
            _held_locks.pop((_lock_tid(), lock_key), None)
            try:
                pid_in_file = int(lock_path.read_text().strip())
                if pid_in_file == my_pid:
                    lock_path.unlink(missing_ok=True)
            except Exception:
                pass


@contextmanager
def write_lock_paper(
    kb_root: Path, paper_key: str, timeout: float = 10.0,
) -> Iterator[Path]:
    """Per-paper advisory lock, POSIX fcntl-based.

    v0.28.0 addition. The kb-root-level `write_lock` serialises
    ALL writers across the whole KB. For operations that only
    touch one md (tag add/remove, ai-zone append, kb_ref
    add/remove, and other RMW paths), per-paper locks let
    different papers make progress concurrently while still
    serialising same-paper writers.

    Lock anchor: `<kb_root>/.kb-mcp/paper-locks/<paper_key>.lock`.
    File persists; fcntl.flock handles cross-process coordination.
    """
    if sys.platform == "win32":
        # On Windows, fall back to the kb-root-level lock. Less
        # parallel but correct. Most users run POSIX.
        with write_lock(kb_root, timeout=timeout):
            yield kb_root / ".kb-mcp" / "paper-locks" / f"{paper_key}.lock"
        return

    import fcntl
    # Defensive: paper_key should be safe (kb-mcp validates md
    # stems) but don't let it escape the locks dir.
    safe_key = re.sub(r"[^A-Za-z0-9_\-]", "_", paper_key)[:200]
    lock_dir = kb_root / ".kb-mcp" / "paper-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_key}.lock"
    lock_key = str(lock_path.resolve())

    # In-process re-entrancy (same paper locked by same process).
    if _held_locks.get((_lock_tid(), lock_key), 0) > 0:
        _held_locks[(_lock_tid(), lock_key)] += 1
        try:
            yield lock_path
        finally:
            _held_locks[(_lock_tid(), lock_key)] -= 1
        return

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"per-paper lock {lock_path} contended; "
                        f"giving up after {timeout}s."
                    )
                time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
    except BaseException:
        os.close(fd)
        raise

    _held_locks[(_lock_tid(), lock_key)] = 1
    _held_lock_fds[(_lock_tid(), lock_key)] = fd
    try:
        yield lock_path
    finally:
        _held_locks[(_lock_tid(), lock_key)] -= 1
        if _held_locks[(_lock_tid(), lock_key)] <= 0:
            _held_locks.pop((_lock_tid(), lock_key), None)
            stored_fd = _held_lock_fds.pop((_lock_tid(), lock_key), fd)
            try:
                fcntl.flock(stored_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(stored_fd)


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
