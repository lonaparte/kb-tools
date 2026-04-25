"""Single-process advisory lock for `kb-importer import`.

v27 addition. Prevents two concurrent imports from racing on the
same KB — which would silently produce interleaved md writes,
double Zotero API traffic, and confusing git history.

Design:
- Lock file at `<kb_root>/.kb-mcp/import.lock`. Contains PID and
  start time as JSON, human-inspectable.
- `fcntl.flock(LOCK_EX | LOCK_NB)` for real mutual exclusion — two
  processes racing through `acquire()` will have exactly one win.
  Lock is released when the file descriptor is closed (on normal
  exit) OR when the OS reaps the process (so a crashed importer
  doesn't wedge future runs).
- Clean API: `with import_lock(kb_root): ...`. Raises
  `ImportLockHeld` if another process holds it; caller bails out
  with a clear message.
- Stale-lock handling: if the file exists but flock succeeds, the
  previous holder crashed and we reuse. If flock fails, we read
  the file for diagnostic (pid / started_at) and include it in the
  error so the user sees *which* process is holding it.

Why flock and not mkdir-based lock: flock is auto-released on
process death, which is the right behaviour here. A directory
lock would require manual cleanup after crashes, and kb-importer
runs long enough (hours for fulltext LLM passes) that a crashed
run leaving a sticky lock is realistic. Caveat: flock is POSIX-
only. On Windows this degrades to "no lock" — acceptable because
the Windows port isn't a release target.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


IMPORT_LOCK_REL = ".kb-mcp/import.lock"


class ImportLockHeld(Exception):
    """Another `kb-importer import` process holds the lock.

    Attributes:
        holder_pid: PID claimed by the lock file, or None if
            unreadable / malformed.
        holder_started_at: ISO timestamp of when the holder started,
            or None.
    """

    def __init__(
        self,
        message: str,
        *,
        holder_pid: int | None = None,
        holder_started_at: str | None = None,
    ):
        super().__init__(message)
        self.holder_pid = holder_pid
        self.holder_started_at = holder_started_at


@contextmanager
def import_lock(kb_root: Path):
    """Context manager that holds an exclusive lock for the duration
    of an import run.

    Usage:
        with import_lock(kb_root):
            ... do the import ...

    Raises ImportLockHeld immediately if another importer is running.
    """
    try:
        import fcntl
    except ImportError:
        # Windows — flock unavailable. We don't raise; the user gets
        # no protection but also no obstruction. Log once so the
        # situation is visible.
        log.warning(
            "kb-importer: fcntl not available on this platform; "
            "skipping import.lock. Two concurrent imports on the "
            "same KB will race.",
        )
        yield
        return

    lock_dir = kb_root / ".kb-mcp"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "import.lock"

    # Open in O_CREAT | O_RDWR so both reading the holder (when we
    # failed) and writing our own identity (when we won) work.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process holds the lock. Read the file for
            # diagnostic — don't re-raise OSError even if the read
            # fails, because the lock-held signal is what matters.
            holder_pid: int | None = None
            holder_started_at: str | None = None
            try:
                data = os.read(fd, 4096).decode("utf-8", errors="replace")
                if data:
                    parsed = json.loads(data)
                    holder_pid = parsed.get("pid")
                    holder_started_at = parsed.get("started_at")
            except (OSError, json.JSONDecodeError):
                pass
            finally:
                os.close(fd)
            msg = (
                f"another kb-importer import is already running on "
                f"{kb_root}"
            )
            if holder_pid:
                msg += f" (pid {holder_pid}"
                if holder_started_at:
                    msg += f", started {holder_started_at}"
                msg += ")"
            msg += (
                ". Wait for it to finish, or if it crashed, "
                f"remove {lock_path} manually."
            )
            raise ImportLockHeld(
                msg,
                holder_pid=holder_pid,
                holder_started_at=holder_started_at,
            )

        # We own the lock. Write our identity (truncate first in
        # case the file had stale content from a crashed run).
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        os.write(fd, json.dumps(payload).encode("utf-8"))
        # Explicit flush so another `cat import.lock` in a second
        # shell immediately sees who holds it.
        os.fsync(fd)

        try:
            yield
        finally:
            # 1.4.2: do NOT unlink the lock file on the success
            # path. Pre-1.4.2 we did, which created a race window:
            #
            #   A: close(fd)            → flock released
            #   B: open(lock_path)      → same inode, flock OK
            #   B: write own PID
            #   A: unlink(lock_path)    → file gone
            #   C: open(lock_path, O_CREAT)  → new inode
            #   C: flock OK             → C and B both think they
            #                             hold the lock simultaneously.
            #
            # kb_write/atomic.py's write_lock has the same warning
            # baked into a long comment; this fix brings import_lock
            # in line. The lock file is small (~80B); leaving it on
            # disk is a tidy concern, not a correctness one.
            #
            # Only truncate-and-close. flock drops automatically on
            # close.
            try:
                os.ftruncate(fd, 0)
                os.close(fd)
            except OSError:
                pass
    except Exception:
        # Any error path that didn't already close the fd.
        try:
            os.close(fd)
        except OSError:
            pass
        raise
