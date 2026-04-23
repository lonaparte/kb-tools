"""After a write, tell kb-mcp to refresh its projection.

We don't import kb-mcp directly — it may not be installed in the
local-agent-only scenario, and even if it is, kb-mcp depends on
sqlite-vec which we want to keep as an optional dep of kb-write.

Instead: shell out to `kb-mcp index` if the binary is on PATH. Fail
quietly if absent (print a hint to stderr so the user knows they
can run it manually).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def trigger_reindex(kb_root: Path, *, enabled: bool = True) -> bool:
    """Run `kb-mcp index` if kb-mcp is installed.

    Returns True if the index command completed successfully; False
    if skipped or failed. Never raises — a reindex failure shouldn't
    abort the user's write (the data is already on disk).
    """
    if not enabled:
        return False
    if shutil.which("kb-mcp") is None:
        log.info(
            "kb-mcp not on PATH; skipping reindex. "
            "Run `kb-mcp index` manually to update search indices."
        )
        return False
    try:
        r = subprocess.run(
            ["kb-mcp", "--kb-root", str(kb_root), "index"],
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
