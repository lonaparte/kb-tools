"""Minimal config for kb_write.

Design rationale: kb_write is client-agnostic. The MCP server
supplies kb_root via its own Config; local CLI uses $KB_ROOT or
--kb-root; Python API callers pass it explicitly. We don't
reinvent YAML config loading here — just the per-operation knobs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WriteContext:
    """Bundled options for a single write operation.

    Any op function in kb_write.ops.* takes a WriteContext plus
    op-specific args. Keeping the options as a value object makes
    it trivial to thread them through MCP tools and CLI alike.
    """
    kb_root: Path
    git_commit: bool = True            # default ON per your rule
    reindex: bool = True               # run `kb-mcp index` after
    lock: bool = True                  # acquire write.lock
    # Optional per-commit message body. Useful when an agent wants
    # to leave a rationale in git log.
    commit_message: str | None = None
    # Set dry_run=True to run validations + diff without writing.
    dry_run: bool = False
    # Who's calling: "cli", "mcp", or "python". Appears in audit
    # log so we can tell which client ran each operation.
    actor: str = "python"
    # Audit log on by default. Turn off only for throwaway test
    # runs — the log is cheap and saves your life on bug reports.
    audit: bool = True

    def __post_init__(self):
        self.kb_root = Path(self.kb_root).expanduser().resolve()


def kb_root_from_env(explicit: Path | None = None) -> Path:
    """Resolve kb_root following the standard precedence:
    explicit arg > $KB_ROOT env var > workspace autodetect > error.

    Workspace autodetect: if this module is installed under a
    `.ee-kb-tools/` directory, look for a sibling `ee-kb/` directory.
    See `kb_write.workspace` for details.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("KB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # Fall through to workspace autodetect.
    try:
        from .workspace import resolve_workspace
        ws = resolve_workspace()
        return ws.kb_root
    except Exception as e:
        raise ValueError(
            "kb_root not set. Provide via --kb-root, $KB_ROOT env var, "
            "or use the canonical workspace layout (.ee-kb-tools/ + "
            f"ee-kb/ siblings).\n  autodetect error: {e}"
        ) from e
