"""Unit tests for kb_core.frontmatter.extract_list.

This helper replaces two divergent regex-based parsers in
kb_importer and kb_write that each had a different bug around
block-form YAML lists. The test cases below are all real-world
shapes observed in user-reported bug #26.5-{2,3}."""
from __future__ import annotations

import pytest

from kb_core.frontmatter import extract_list


# Exact shape python-frontmatter / PyYAML default-dump produces
# for a list-of-strings value. Captured from a real kb-importer
# write observed in bug report 26.5.
REAL_KB_IMPORTER_SHAPE = """\
zotero_key: ABCD1234
zotero_version: 42
kind: paper
title: Example Paper
authors:
- Alice Smith
- Bob Jones
year: 2024
zotero_attachment_keys:
- 5N6FQXJJ
- UUZRAV8C
kb_tags:
- topic-a
- topic-b"""


class TestBlockForm:
    def test_real_kb_importer_shape_attachments(self):
        # The exact bug #26.5-2 case: 0-indent block items.
        got = extract_list(REAL_KB_IMPORTER_SHAPE, "zotero_attachment_keys")
        assert got == ["5N6FQXJJ", "UUZRAV8C"], (
            "regression: 0-indent block-form list not parsed — "
            "re-summarize would find no PDF for any real paper"
        )

    def test_real_kb_importer_shape_authors(self):
        got = extract_list(REAL_KB_IMPORTER_SHAPE, "authors")
        assert got == ["Alice Smith", "Bob Jones"]

    def test_real_kb_importer_shape_tags(self):
        got = extract_list(REAL_KB_IMPORTER_SHAPE, "kb_tags")
        assert got == ["topic-a", "topic-b"]

    def test_two_space_indent_also_works(self):
        # Some YAML emitters / hand-edits use the indented form.
        # Both must parse identically.
        fm = "key:\n  - a\n  - b\n"
        assert extract_list(fm, "key") == ["a", "b"]

    def test_quoted_values_stripped(self):
        fm = 'key:\n- "quoted"\n- \'single\'\n'
        assert extract_list(fm, "key") == ["quoted", "single"]


class TestFlowForm:
    def test_flow_inline(self):
        fm = "key: [a, b, c]"
        assert extract_list(fm, "key") == ["a", "b", "c"]

    def test_flow_with_quoted(self):
        fm = 'key: ["a b", "c d"]'
        assert extract_list(fm, "key") == ["a b", "c d"]

    def test_flow_with_spaces(self):
        fm = "key:   [  a ,  b  ]  "
        assert extract_list(fm, "key") == ["a", "b"]


class TestMissingOrMalformed:
    def test_missing_key_returns_empty(self):
        fm = "other: x\n"
        assert extract_list(fm, "key") == []

    def test_scalar_value_returns_empty(self):
        # `key: value` (not a list) → empty, don't crash.
        fm = "key: just-a-scalar\n"
        assert extract_list(fm, "key") == []

    def test_empty_frontmatter(self):
        assert extract_list("", "key") == []

    def test_list_stops_at_next_top_level_key(self):
        fm = "key:\n- a\n- b\nother: xyz\n"
        assert extract_list(fm, "key") == ["a", "b"]

    def test_blank_line_mid_list_tolerated(self):
        fm = "key:\n- a\n\n- b\n"
        assert extract_list(fm, "key") == ["a", "b"]


class TestBackwardCompat:
    """Both the old 2-space-indent form AND the new 0-indent form
    must coexist, because users have 1000+ mds written across
    multiple kb-importer versions. Parser must handle both
    interchangeably."""

    def test_mixed_document(self):
        fm = (
            "key1:\n"
            "- flat_a\n"
            "- flat_b\n"
            "key2:\n"
            "  - indented_a\n"
            "  - indented_b\n"
        )
        assert extract_list(fm, "key1") == ["flat_a", "flat_b"]
        assert extract_list(fm, "key2") == ["indented_a", "indented_b"]
