"""Regression tests for the v25 heading-based self-heal and its
v27 boundary fix.

Context: pre-v21 md files don't have `<!-- kb-fulltext-start -->`
markers, but many have a `## AI Summary (from Full Text)` heading.
v25 added a self-heal that extracts the fulltext region by that
heading. The v25 implementation used any bare `---` line as the
end sentinel — which over-matched: markdown horizontal rules INSIDE
the summary (section breaks, table separators rendered with
hyphens) triggered early cut-off, silently losing the rest of the
summary.

v27 fix: require the `---` line to be IMMEDIATELY followed by
`<!-- kb-ai-zone-start -->` (the unambiguous AI-zone opener), or
stop at EOF. These tests lock that behaviour."""
from __future__ import annotations

import pytest


def _skip_if_no_frontmatter():
    try:
        import frontmatter  # noqa: F401
    except ImportError:
        pytest.skip("python-frontmatter not installed; md_io requires it")


def _load():
    _skip_if_no_frontmatter()
    from kb_importer.md_io import (
        _extract_legacy_fulltext_by_heading as extract,
        AI_ZONE_START as ai_zone,
    )
    return extract, ai_zone


def _body(lines):
    return "\n".join(lines)


class TestLegacyFulltextExtraction:
    def test_heading_not_found_returns_none(self):
        extract, _ai_zone = _load()
        body = _body(["# Some other header", "body text"])
        assert extract(body) is None

    def test_simple_extraction_to_eof(self):
        extract, _ai_zone = _load()
        body = _body([
            "## AI Summary (from Full Text)",
            "Section 1 body text.",
            "Section 2 body text.",
        ])
        got = extract(body)
        assert got == "Section 1 body text.\nSection 2 body text."

    def test_ai_zone_sentinel_terminates(self):
        extract, ai_zone = _load()
        body = _body([
            "## AI Summary (from Full Text)",
            "fulltext content here",
            "---",
            ai_zone,
            "agent notes here",
            "<!-- kb-ai-zone-end -->",
        ])
        got = extract(body)
        assert got == "fulltext content here"

    def test_internal_horizontal_rule_not_cut(self):
        """The v27 boundary fix. A bare `---` INSIDE the summary
        (not followed by the AI-zone start) must NOT truncate —
        that's the v25 over-match bug."""
        extract, _ai_zone = _load()
        body = _body([
            "## AI Summary (from Full Text)",
            "Section 1 text.",
            "",
            "---",              # markdown horizontal rule in summary
            "",
            "Section 2 text.",  # must still be in the output
            "",
            "---",
            "Section 3 text.",
        ])
        got = extract(body)
        assert "Section 2 text." in got, (
            "v27 regression: internal `---` truncated summary (v25 bug)"
        )
        assert "Section 3 text." in got, (
            "v27 regression: second internal `---` truncated summary"
        )

    def test_horizontal_rule_then_other_content_not_cut(self):
        """Similar — `---` followed by arbitrary content (not the
        AI-zone marker) is an in-body rule, not the AI-zone boundary."""
        extract, _ai_zone = _load()
        body = _body([
            "## AI Summary (from Full Text)",
            "body before rule",
            "---",
            "just more prose here",
            "the end",
        ])
        got = extract(body)
        assert "just more prose here" in got
        assert "the end" in got

    def test_leading_and_trailing_blanks_stripped(self):
        extract, _ai_zone = _load()
        body = _body([
            "## AI Summary (from Full Text)",
            "",
            "",
            "content",
            "",
            "",
        ])
        got = extract(body)
        assert got == "content"

    def test_heading_trailing_whitespace_tolerated(self):
        extract, _ai_zone = _load()
        # Exact heading + trailing whitespace is a common legacy
        # artefact (auto-saved editors).
        body = "## AI Summary (from Full Text)   \ncontent\n"
        got = extract(body)
        assert got == "content"

    def test_ai_zone_sentinel_takes_precedence_over_eof(self):
        """When both patterns would match, the sentinel wins."""
        extract, ai_zone = _load()
        body = _body([
            "## AI Summary (from Full Text)",
            "in scope",
            "---",
            ai_zone,
            "NOT in scope — in AI zone",
        ])
        got = extract(body)
        assert got == "in scope"
        assert "NOT in scope" not in got
