"""Append-only audit log for kb-write operations.

Purpose: git history tells you the content change but not the agent
or context that produced it. This log records one JSON line per
successful write, so you can trace "what tool / when / which md".

Format: JSON Lines at `<kb_root>/.kb-mcp/audit.log`. Example:

    {"ts":"2026-04-22T14:02:11.234Z","actor":"cli",
     "op":"create_thought","target":"thoughts/2026-04-22-foo",
     "mtime_after":1745414531.234,"git_sha":"abc123",
     "reindexed":true}

Design:
- Append-only; rotation is the user's problem (< 1 MB/year typical).
- Each line is a complete JSON object — partial writes at crash
  leave the log valid minus the last op.
- Log failures silently swallow — audit must never break writes.
- Small writes (< PIPE_BUF = 4096B) under O_APPEND are atomic on
  POSIX; our entries are ~250 bytes.

v27 — host-identity fields default OFF:

  Prior versions unconditionally recorded `pid` and `user`. That
  was fine for a private single-user machine, but this log is
  world-readable inside the KB and is included in `kb-mcp snapshot
  export` tars. Sharing or inadvertently publishing a snapshot
  would leak the exporter's Unix username and historical pid
  sequence.

  Default behaviour now: record neither. Opt in via env var when
  the extra context is useful for local debugging:

      export KB_WRITE_AUDIT_INCLUDE_USER=1
      export KB_WRITE_AUDIT_INCLUDE_PID=1

  The log format remains backward-compatible: old entries with
  user/pid still parse; new entries simply lack those keys.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


AUDIT_REL_PATH = ".kb-mcp/audit.log"


def _env_flag(name: str) -> bool:
    """Treat common truthy values as True, everything else False."""
    val = (os.environ.get(name) or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def record(
    kb_root: Path,
    *,
    op: str,
    target: str,
    actor: str = "python",
    mtime_before: float | None = None,
    mtime_after: float | None = None,
    git_sha: str | None = None,
    reindexed: bool | None = None,
    note: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append a structured line to `.kb-mcp/audit.log`. Never raises.

    Args:
        kb_root: KB root; log goes in `.kb-mcp/audit.log`.
        op: operation name, e.g. "create_thought", "append_ai_zone".
        target: KB-relative path of the affected md.
        actor: "cli", "mcp", or "python". From WriteContext.actor.
        mtime_before: mtime the caller expected pre-write (update
            ops). Lets auditors see the conflict-guard value.
        mtime_after: mtime post-write (0 for delete).
        git_sha: commit hash if auto-commit ran.
        reindexed: whether `kb-mcp index` was called.
        note: free-form string (agent name, extra context, ...).
        extra: extra dict merged shallowly into the entry.
    """
    try:
        entry: dict = {
            "ts": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "actor": actor,
            "op": op,
            "target": target,
        }
        # pid / user are opt-in (see module docstring) to avoid
        # leaking host identity via shared snapshots.
        if _env_flag("KB_WRITE_AUDIT_INCLUDE_PID"):
            entry["pid"] = os.getpid()
        if mtime_before is not None:
            # Nanosecond precision (9 decimals) matches the format used
            # by read_md's mtime header and CLI output, so audit entries
            # can be correlated with the values users copy-paste between
            # calls. Previously rounded to 3 decimals, which was too
            # coarse: a float parsed from ".3f" output wouldn't equal
            # the original mtime and conflict detection fired spuriously.
            entry["mtime_before"] = round(float(mtime_before), 9)
        if mtime_after is not None:
            entry["mtime_after"] = round(float(mtime_after), 9)
        if git_sha:
            entry["git_sha"] = git_sha
        if reindexed is not None:
            entry["reindexed"] = bool(reindexed)
        if _env_flag("KB_WRITE_AUDIT_INCLUDE_USER"):
            # v0.27.4: use getpass.getuser() — it walks LOGNAME /
            # USER / LNAME / USERNAME in that order and falls back
            # to pwd lookup by euid. Prior version tried
            # `os.getlogin()` first then `$USER`; both fail on a
            # stripped-down shell environment (Claude Code / CI /
            # container) where USER is empty but LOGNAME is set.
            # Observed in field testing: audit log recorded
            # `"user": "unknown"` even with the opt-in env var on,
            # making the feature pointless in ~1/3 of real
            # environments.
            import getpass as _getpass
            try:
                entry["user"] = _getpass.getuser()
            except (KeyError, OSError):
                # Very exotic — no env vars AND no pwd entry for
                # the euid. Preserve backwards-compat sentinel.
                entry["user"] = "unknown"
        if note:
            # 1.4.2: truncate caller-supplied free-form fields. The
            # module docstring above promises atomic single-line
            # append for entries < PIPE_BUF (4096B on Linux); a
            # caller passing a multi-KB note could push the encoded
            # line over the boundary and break the atomicity
            # guarantee under concurrent writes. events.jsonl
            # already truncates `detail` to 500B; mirror that here.
            entry["note"] = note[:1000]
        if extra:
            for k, v in extra.items():
                if k not in entry:
                    # Stringify+truncate non-trivial values so a
                    # caller stuffing a multi-MB blob in `extra`
                    # can't blow up the audit line either. Trivial
                    # types (bool / int / small str) round-trip
                    # losslessly through this clamp.
                    if isinstance(v, str) and len(v) > 1000:
                        entry[k] = v[:1000]
                    else:
                        entry[k] = v

        log_dir = Path(kb_root) / ".kb-mcp"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "audit.log"
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # Final belt-and-braces: if the encoded line is still over
        # PIPE_BUF, log a warning rather than silently violating the
        # docstring's atomicity claim. The line is still written
        # (data preservation > docstring).
        if len(line.encode("utf-8")) > 4096:
            log.warning(
                "audit line exceeds PIPE_BUF (%d bytes); single-write "
                "atomicity is not guaranteed under concurrent writes.",
                len(line),
            )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def tail(kb_root: Path, n: int = 50) -> list[dict]:
    """Read the last n entries (parsed). Malformed lines skipped.

    For the `kb-write log` CLI command.
    """
    log_path = Path(kb_root) / AUDIT_REL_PATH
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
