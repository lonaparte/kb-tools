"""Unit tests for kb_importer.events — event recording + JSONL
round-trip.

Covers: events.jsonl is under .kb-mcp/, each event type has the
expected shape, record_event never raises, iter_events filters
correctly."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_importer.events import (
    record_event,
    read_events,
    EVENT_FULLTEXT_SKIP, EVENT_RE_READ, EVENT_RE_SUMMARIZE,
    EVENT_IMPORT_RUN, EVENT_CITATIONS_RUN, EVENT_INDEX_OP,
    REASON_PDF_MISSING,
    RE_READ_SUCCESS,
    IMPORT_RUN_OK,
    INDEX_OP_OK,
)
from kb_core.schema import EVENTS_LOG_REL


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / ".kb-mcp").mkdir()
    return tmp_path


def test_event_types_count():
    # 6 event types as of v26; regression test.
    types = {EVENT_FULLTEXT_SKIP, EVENT_RE_READ, EVENT_RE_SUMMARIZE,
             EVENT_IMPORT_RUN, EVENT_CITATIONS_RUN, EVENT_INDEX_OP}
    assert len(types) == 6


def test_record_fulltext_skip(kb):
    record_event(
        kb, event_type=EVENT_FULLTEXT_SKIP,
        paper_key="ABC", category=REASON_PDF_MISSING,
        detail="missing attachment",
    )
    events = read_events(kb)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == EVENT_FULLTEXT_SKIP
    assert e["paper_key"] == "ABC"
    assert e["category"] == REASON_PDF_MISSING
    assert "ts" in e


def test_events_file_path(kb):
    record_event(
        kb, event_type=EVENT_INDEX_OP,
        category=INDEX_OP_OK, detail="test",
    )
    # Path must match kb_core.schema.EVENTS_LOG_REL so downstream
    # consumers (kb-mcp report) find it.
    assert (kb / EVENTS_LOG_REL).is_file()


def test_record_never_raises_on_bad_kb_root(tmp_path):
    # If kb_root doesn't exist / isn't writable, record_event is
    # best-effort — it must not raise. (Calling code doesn't check
    # its return value.)
    nowhere = tmp_path / "does-not-exist-ever"
    record_event(
        nowhere, event_type=EVENT_FULLTEXT_SKIP,
        paper_key="X", category=REASON_PDF_MISSING,
    )  # must not raise


def test_multiple_events_round_trip(kb):
    for i in range(5):
        record_event(
            kb, event_type=EVENT_RE_READ,
            paper_key=f"K{i}", category=RE_READ_SUCCESS,
        )
    events = read_events(kb)
    assert len(events) == 5
    keys = [e["paper_key"] for e in events]
    assert keys == [f"K{i}" for i in range(5)]


def test_jsonl_format_one_event_per_line(kb):
    # Each entry is a complete JSON object on its own line.
    record_event(
        kb, event_type=EVENT_IMPORT_RUN,
        category=IMPORT_RUN_OK, detail="x",
    )
    record_event(
        kb, event_type=EVENT_IMPORT_RUN,
        category=IMPORT_RUN_OK, detail="y",
    )
    text = (kb / EVENTS_LOG_REL).read_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # must not raise


def test_filter_by_event_type(kb):
    record_event(kb, event_type=EVENT_FULLTEXT_SKIP, paper_key="A", category=REASON_PDF_MISSING)
    record_event(kb, event_type=EVENT_RE_READ,       paper_key="B", category=RE_READ_SUCCESS)
    record_event(kb, event_type=EVENT_FULLTEXT_SKIP, paper_key="C", category=REASON_PDF_MISSING)

    only_skips = read_events(kb, event_types=[EVENT_FULLTEXT_SKIP])
    assert len(only_skips) == 2
    assert [e["paper_key"] for e in only_skips] == ["A", "C"]
