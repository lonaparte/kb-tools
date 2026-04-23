"""kb_write.paths — v27 compatibility shim.

The path-safety helpers (`PathError`, `safe_resolve`, `to_relative`,
`NodeAddress`, `parse_target`, `from_md_path`), the KB layout
constants (`PAPERS_DIR`, `TOPICS_STANDALONE_DIR`, `TOPICS_AGENT_DIR`,
`THOUGHTS_DIR`, `ACTIVE_SUBDIRS`), and `is_book_chapter_filename`
were moved to the `kb_core` package in v27. This module now re-
exports them so existing `from kb_write.paths import ...` calls
in downstream code continue to work.

Don't add new symbols here — put them in kb_core and re-export if
absolutely necessary for backward compatibility. The intent of v27
is to stop this module from accumulating its own logic.
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
