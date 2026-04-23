"""kb_mcp.paths — v27 compatibility shim.

All path helpers moved to `kb_core` in v27. This module re-exports
them so existing `from kb_mcp.paths import ...` or
`from ..paths import ...` calls inside kb_mcp keep working. See
kb_write.paths for the matching shim on the write side; kb_core/
paths.py + addressing.py are the real implementations.

Historical note: v26 kept this module slimmer than kb_write.paths
(no NodeAddress / parse_target on the read-only side). We now
export everything from kb_core uniformly — the read side gets
NodeAddress access at essentially zero cost, which removes a long-
standing asymmetry where kb_mcp indexer code had to re-derive
(node_type, key) from a path by pattern-matching instead of just
calling `from_md_path`.
"""
from __future__ import annotations

from kb_core.paths import (
    PathError,
    safe_resolve,
    to_relative,
    is_book_chapter_filename,
    PAPERS_DIR,
    TOPICS_STANDALONE_DIR,
    TOPICS_AGENT_DIR,
    THOUGHTS_DIR,
    ACTIVE_SUBDIRS,
)
from kb_core.addressing import (
    NodeAddress,
    parse_target,
    from_md_path,
)

__all__ = [
    "PathError",
    "safe_resolve",
    "to_relative",
    "is_book_chapter_filename",
    "PAPERS_DIR",
    "TOPICS_STANDALONE_DIR",
    "TOPICS_AGENT_DIR",
    "THOUGHTS_DIR",
    "ACTIVE_SUBDIRS",
    "NodeAddress",
    "parse_target",
    "from_md_path",
]
