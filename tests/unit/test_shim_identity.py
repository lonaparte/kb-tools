"""Sanity tests that kb_write / kb_mcp shims re-export kb_core
symbols by identity.

Same check as scripts/check_package_consistency.py but with a
friendlier failure mode under pytest. When one fails, it points at
the specific symbol that diverged, so you don't have to eyeball
the lint output."""
from __future__ import annotations

import pytest

import kb_core
import kb_core.addressing as core_addr
import kb_core.paths as core_paths
import kb_core.workspace as core_ws
import kb_mcp.paths as mcp_paths
import kb_mcp.workspace as mcp_ws
import kb_write.paths as write_paths
import kb_write.workspace as write_ws


PATHS_SYMBOLS = (
    "PathError", "safe_resolve", "to_relative",
    "is_book_chapter_filename",
    "PAPERS_DIR", "TOPICS_STANDALONE_DIR", "TOPICS_AGENT_DIR",
    "THOUGHTS_DIR", "ACTIVE_SUBDIRS",
    "NodeAddress", "parse_target", "from_md_path",
)
WORKSPACE_SYMBOLS = (
    "Workspace", "WorkspaceError", "resolve_workspace", "find_tools_dir",
    "TOOLS_DIR_NAME", "KB_DIR_NAME", "ZOTERO_DIR_NAME",
    "ZOTERO_STORAGE_SUBDIR",
)


def _canonical_paths_symbol(name: str):
    """Return the kb_core object for NAME, from paths.py or
    addressing.py (whichever defines it)."""
    if hasattr(core_paths, name):
        return getattr(core_paths, name)
    if hasattr(core_addr, name):
        return getattr(core_addr, name)
    pytest.fail(f"kb_core has no symbol {name!r} — test list is stale")


@pytest.mark.parametrize("symbol", PATHS_SYMBOLS)
def test_kb_write_paths_shim_identity(symbol):
    canonical = _canonical_paths_symbol(symbol)
    assert getattr(write_paths, symbol) is canonical, (
        f"kb_write.paths.{symbol} is not the kb_core object — shim "
        f"drift"
    )


@pytest.mark.parametrize("symbol", PATHS_SYMBOLS)
def test_kb_mcp_paths_shim_identity(symbol):
    canonical = _canonical_paths_symbol(symbol)
    assert getattr(mcp_paths, symbol) is canonical, (
        f"kb_mcp.paths.{symbol} is not the kb_core object — shim "
        f"drift"
    )


@pytest.mark.parametrize("symbol", WORKSPACE_SYMBOLS)
def test_kb_write_workspace_shim_identity(symbol):
    canonical = getattr(core_ws, symbol)
    assert getattr(write_ws, symbol) is canonical


@pytest.mark.parametrize("symbol", WORKSPACE_SYMBOLS)
def test_kb_mcp_workspace_shim_identity(symbol):
    canonical = getattr(core_ws, symbol)
    assert getattr(mcp_ws, symbol) is canonical


def test_patherror_is_single_class():
    # The whole point of kb_core: one PathError class, not three.
    assert write_paths.PathError is mcp_paths.PathError
    assert write_paths.PathError is core_paths.PathError
    assert write_paths.PathError is kb_core.PathError


def test_nodeaddress_is_single_class():
    assert write_paths.NodeAddress is mcp_paths.NodeAddress
    assert write_paths.NodeAddress is core_addr.NodeAddress
