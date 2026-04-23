"""Trivial checks for kb_core.schema constants. Mostly a
regression gate — if someone changes SCHEMA_VERSION or the
fulltext markers by accident, this fires."""
from __future__ import annotations

import kb_core
from kb_core.schema import (
    SCHEMA_VERSION,
    EVENTS_LOG_REL,
    AUDIT_LOG_REL,
    FULLTEXT_START,
    FULLTEXT_END,
    SECTION_COUNT,
)


def test_schema_version_is_7():
    # v27 ships schema v7: v6 had broken foreign keys on side
    # tables (targeted papers.zotero_key which isn't PK/UNIQUE
    # since the v6 PK change). v7 fixes the FK targets to
    # papers.paper_key. A v6 DB on disk is rebuilt on first v7
    # startup. Bumping this constant without a migration would
    # break users' existing DBs silently — coordinate with
    # kb_mcp.store.EXPECTED_SCHEMA_VERSION.
    assert SCHEMA_VERSION == 7


def test_kb_mcp_store_agrees():
    # Cross-check: the EXPECTED_SCHEMA_VERSION in kb_mcp.store must
    # match kb_core's constant. If someone bumps one and forgets the
    # other, this catches it locally before E2E.
    from kb_mcp.store import EXPECTED_SCHEMA_VERSION
    assert EXPECTED_SCHEMA_VERSION == SCHEMA_VERSION


def test_log_paths_under_kb_mcp():
    assert EVENTS_LOG_REL == ".kb-mcp/events.jsonl"
    assert AUDIT_LOG_REL == ".kb-mcp/audit.log"


def test_fulltext_markers_are_html_comments():
    # Must be valid markdown that's invisible when rendered, and
    # byte-stable across packages. HTML comments hit all three.
    assert FULLTEXT_START.startswith("<!--")
    assert FULLTEXT_START.endswith("-->")
    assert FULLTEXT_END.startswith("<!--")
    assert FULLTEXT_END.endswith("-->")


def test_section_count_is_seven():
    # 7-section summary template; re_summarize splices assume
    # exactly this count.
    assert SECTION_COUNT == 7


def test_top_level_reexports():
    # The top-level kb_core namespace should re-export everything —
    # convenience for `from kb_core import ...` without knowing
    # which submodule owns what.
    assert kb_core.SCHEMA_VERSION == SCHEMA_VERSION
    assert kb_core.FULLTEXT_START == FULLTEXT_START
    assert kb_core.SECTION_COUNT == SECTION_COUNT
