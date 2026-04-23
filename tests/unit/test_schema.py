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


def test_schema_version_is_6():
    # v27 ships with schema v6 unchanged — coordinate a bump here
    # with kb_mcp.store.EXPECTED_SCHEMA_VERSION and a migration.
    assert SCHEMA_VERSION == 6


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
