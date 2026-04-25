"""After a write, tell kb-mcp to refresh its projection.

We don't import kb-mcp directly — it may not be installed in the
local-agent-only scenario, and even if it is, kb-mcp depends on
sqlite-vec which we want to keep as an optional dep of kb-write.

Instead: shell out to `kb-mcp index` via the absolute path of the
binary. Fail quietly if absent.

1.4.2 hardening: do NOT trust bare `kb-mcp` on PATH. If the user's
PATH happens to include a directory with a malicious `kb-mcp`
binary (rogue build, attacker-writable dir prepended to PATH, etc.),
every write would silently execute it. Resolution order, in
priority:

  1. `<workspace>/.ee-kb-tools/.venv/bin/kb-mcp` — the canonical
     deploy.sh layout. We trust this path absolutely because the
     workspace owner controls it.
  2. The same Python interpreter's bin dir (sys.executable's parent),
     so a `pip install -e ./kb_mcp` into a developer venv keeps
     working without the deployed layout.
  3. `shutil.which("kb-mcp")` — last resort. We log the resolved
     ABSOLUTE PATH at INFO level so anyone tailing logs can spot a
     suspicious resolution before damage compounds.

The subprocess is then invoked with the ABSOLUTE path, so even if
PATH is mutated mid-run we use the path we resolved.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _resolve_kb_mcp(kb_root: Path) -> str | None:
    """Return the absolute path of a trusted kb-mcp binary, or None.

    See module docstring for resolution priority. We accept the
    canonical workspace layout first because that's what we
    explicitly control via `kb-write init` / `scripts/deploy.sh`.
    """
    # (1) Workspace-local venv.
    candidate = (
        kb_root.parent / ".ee-kb-tools" / ".venv" / "bin" / "kb-mcp"
    )
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    # (1b) Windows variant — venv puts scripts in Scripts/ not bin/.
    candidate_win = (
        kb_root.parent / ".ee-kb-tools" / ".venv" / "Scripts" / "kb-mcp.exe"
    )
    if candidate_win.is_file() and os.access(candidate_win, os.X_OK):
        return str(candidate_win)

    # (2) Same-venv-as-this-process. sys.executable's directory
    # contains the kb-mcp script if pip installed it there.
    py_dir = Path(sys.executable).parent
    for name in ("kb-mcp", "kb-mcp.exe"):
        c = py_dir / name
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)

    # (3) PATH fallback. Log loudly if we land here so a malicious
    # PATH-shadowed kb-mcp is at least visible in logs.
    via_path = shutil.which("kb-mcp")
    if via_path:
        log.info(
            "kb-mcp resolved via PATH at %s (not in workspace venv "
            "or current Python's bin dir). Verify this is the binary "
            "you expect.",
            via_path,
        )
        return via_path

    return None


def trigger_reindex(kb_root: Path, *, enabled: bool = True) -> bool:
    """Run `kb-mcp index` if kb-mcp is installed.

    Returns True if the index command completed successfully; False
    if skipped or failed. Never raises — a reindex failure shouldn't
    abort the user's write (the data is already on disk).
    """
    if not enabled:
        return False
    kb_mcp_path = _resolve_kb_mcp(kb_root)
    if kb_mcp_path is None:
        log.info(
            "kb-mcp not found in workspace venv, current Python's "
            "bin dir, or PATH; skipping reindex. Run `kb-mcp index` "
            "manually to update search indices."
        )
        return False
    try:
        r = subprocess.run(
            [kb_mcp_path, "--kb-root", str(kb_root), "index"],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            log.warning(
                "kb-mcp index failed (rc=%d): %s",
                r.returncode, r.stderr.strip()[:500],
            )
            return False
        log.debug("kb-mcp index ok: %s", r.stdout.strip()[:200])
        return True
    except subprocess.TimeoutExpired:
        log.warning("kb-mcp index timed out after 5 minutes.")
        return False
    except Exception as e:
        log.warning("kb-mcp index raised: %s", e)
        return False
