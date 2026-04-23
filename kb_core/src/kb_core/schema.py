"""Schema / format version constants shared across the toolchain.

These are the numbers that MUST agree between packages in one
release. Bumping one is equivalent to a schema migration and is
coordinated across all packages in the bundle.
"""
from __future__ import annotations

# SQLite projection DB schema version — see kb_mcp.store for the
# authoritative current schema and kb_mcp.migrations for migrations.
# When this bumps, every package that reads the DB must pin to the
# new kb-mcp.
SCHEMA_VERSION = 6


# Well-known file paths inside `<kb_root>/.kb-mcp/` that multiple
# packages read or write. Kept here so a rename happens in exactly
# one place.
EVENTS_LOG_REL = ".kb-mcp/events.jsonl"
AUDIT_LOG_REL  = ".kb-mcp/audit.log"


# Marker comments that delimit the fulltext summary region in a
# paper md. kb_importer writes them; kb_write.ops.re_summarize finds
# and splices between them; kb_mcp.indexer parses sections out of
# them. All three need to agree byte-for-byte.
FULLTEXT_START = "<!-- kb-fulltext-start -->"
FULLTEXT_END   = "<!-- kb-fulltext-end -->"

# Number of sections inside the fulltext region (see
# kb_importer.templates.ai_summary_prompt.md). Used by
# kb_write.ops.re_summarize to validate the LLM splice.
SECTION_COUNT = 7
