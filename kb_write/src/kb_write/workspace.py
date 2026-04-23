"""kb_write.workspace — v27 compatibility shim.

Workspace resolution moved to `kb_core.workspace` in v27. This
module re-exports so existing `from kb_write.workspace import ...`
calls continue to work.
"""
from __future__ import annotations

from kb_core.workspace import (
    TOOLS_DIR_NAME,
    KB_DIR_NAME,
    ZOTERO_DIR_NAME,
    ZOTERO_STORAGE_SUBDIR,
    WorkspaceError,
    Workspace,
    find_tools_dir,
    resolve_workspace,
)

__all__ = [
    "TOOLS_DIR_NAME",
    "KB_DIR_NAME",
    "ZOTERO_DIR_NAME",
    "ZOTERO_STORAGE_SUBDIR",
    "WorkspaceError",
    "Workspace",
    "find_tools_dir",
    "resolve_workspace",
]
