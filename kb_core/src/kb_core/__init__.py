"""kb_core — shared contract layer for the ee-kb toolchain.

See README.md for scope. Strictly: constants + pure functions, no
business logic, no third-party deps, no imports from other ee-kb
packages.
"""
from __future__ import annotations

__version__ = "0.29.2"

# Re-exports so downstream can write `from kb_core import safe_resolve`.
from .paths import (
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
from .addressing import (
    NodeAddress,
    parse_target,
    from_md_path,
)
from .schema import (
    SCHEMA_VERSION,
    EVENTS_LOG_REL,
    AUDIT_LOG_REL,
    FULLTEXT_START,
    FULLTEXT_END,
    SECTION_COUNT,
)
from .workspace import (
    find_workspace_root,
    find_kb_root,
    find_tools_dir,
    Workspace,
    WorkspaceError,
    resolve_workspace,
    TOOLS_DIR_NAME,
    KB_DIR_NAME,
    ZOTERO_DIR_NAME,
    ZOTERO_STORAGE_SUBDIR,
)
from .format import (
    render_path,
    render_error,
    render_json,
    WRITE_RESULT_FIELD_ORDER,
)
from .frontmatter import (
    extract_list,
)

__all__ = [
    "__version__",
    # paths
    "PathError",
    "safe_resolve",
    "to_relative",
    "is_book_chapter_filename",
    "PAPERS_DIR",
    "TOPICS_STANDALONE_DIR",
    "TOPICS_AGENT_DIR",
    "THOUGHTS_DIR",
    "ACTIVE_SUBDIRS",
    # addressing
    "NodeAddress",
    "parse_target",
    "from_md_path",
    # schema
    "SCHEMA_VERSION",
    "EVENTS_LOG_REL",
    "AUDIT_LOG_REL",
    "FULLTEXT_START",
    "FULLTEXT_END",
    "SECTION_COUNT",
    # workspace
    "find_workspace_root",
    "find_kb_root",
    "find_tools_dir",
    "Workspace",
    "WorkspaceError",
    "resolve_workspace",
    "TOOLS_DIR_NAME",
    "KB_DIR_NAME",
    "ZOTERO_DIR_NAME",
    "ZOTERO_STORAGE_SUBDIR",
    # format
    "render_path",
    "render_error",
    "render_json",
    "WRITE_RESULT_FIELD_ORDER",
    # frontmatter
    "extract_list",
]
