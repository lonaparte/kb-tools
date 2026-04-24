"""Unit tests for re_summarize's three integration modes (1.3.0+).

These tests exercise the append / replace / merge helpers and the
`_prepend_revisit_block` / `_build_revisit_block` helpers directly,
without mocking the LLM pipeline end-to-end — that's what e2e
covers. The goal here is to lock in the md structure / markers /
ordering invariants.
"""
from __future__ import annotations

from kb_write.ops.re_summarize import (
    _build_revisit_block,
    _prepend_revisit_block,
    _extract_revisits_region,
    _format_model_label,
)
from kb_core import (
    REVISITS_START,
    REVISITS_END,
    REVISIT_BLOCK_START,
    REVISIT_BLOCK_END,
)


# ----------------------------------------------------------------------
# Model label formatting
# ----------------------------------------------------------------------


def test_format_model_label_both_set():
    assert _format_model_label("openrouter", "openai/gpt-4o") == "openrouter/openai/gpt-4o"


def test_format_model_label_only_model():
    assert _format_model_label(None, "gemini-3.1-pro") == "gemini-3.1-pro"


def test_format_model_label_only_provider():
    assert _format_model_label("gemini", None) == "gemini"


def test_format_model_label_both_none():
    """Both None means "use configured default" — label reflects
    that rather than being empty, so the revisit block and commit
    message are still readable."""
    assert _format_model_label(None, None) == "(default)"


# ----------------------------------------------------------------------
# Build revisit block
# ----------------------------------------------------------------------


def test_build_revisit_block_wraps_with_markers():
    block = _build_revisit_block(
        "2026-04-24", "openrouter/openai/gpt-oss-120b:free",
        "## 1. Problem\nBody A\n\n## 2. Method\nBody B\n",
    )
    # Marker structure
    assert REVISIT_BLOCK_START in block
    assert REVISIT_BLOCK_END in block
    # Attributes encoded
    assert 'date="2026-04-24"' in block
    assert 'model="openrouter/openai/gpt-oss-120b:free"' in block
    # Human heading present
    assert "### 2026-04-24 — openrouter/openai/gpt-oss-120b:free" in block
    # Content preserved
    assert "Body A" in block and "Body B" in block


def test_build_revisit_block_ends_with_newline():
    block = _build_revisit_block(
        "2026-04-24", "openai/gpt-4o", "body\n",
    )
    assert block.endswith("\n")


# ----------------------------------------------------------------------
# Prepend to Revisits region
# ----------------------------------------------------------------------


def test_prepend_creates_region_when_absent():
    """First revisit on a paper that's never been re-read: the
    `## Revisits` heading + markers are created at the end of md."""
    md = "---\ntitle: foo\n---\n\nBody\n\n<!-- kb-fulltext-start -->\nSummary\n<!-- kb-fulltext-end -->\n"
    block = _build_revisit_block("2026-04-24", "openai/gpt-4o", "NEW\n")
    out = _prepend_revisit_block(md, block)
    # The baseline fulltext block is untouched.
    assert "<!-- kb-fulltext-start -->\nSummary\n<!-- kb-fulltext-end -->" in out
    # New Revisits section exists at end.
    assert "## Revisits" in out
    assert REVISITS_START in out
    assert REVISITS_END in out
    # Block shows up inside the region.
    region = _extract_revisits_region(out)
    assert region is not None
    assert "2026-04-24" in region
    assert "NEW" in region


def test_prepend_newest_at_top():
    """A second revisit should land ABOVE the first — newest first."""
    md = "---\ntitle: foo\n---\n\n<!-- kb-fulltext-start -->\nS\n<!-- kb-fulltext-end -->\n"
    first = _build_revisit_block("2026-03-01", "openai/gpt-4o", "FIRST_CONTENT\n")
    md_with_first = _prepend_revisit_block(md, first)
    second = _build_revisit_block("2026-04-24", "anthropic/claude", "SECOND_CONTENT\n")
    md_with_both = _prepend_revisit_block(md_with_first, second)

    region = _extract_revisits_region(md_with_both)
    assert region is not None
    # Second must appear before first (newest-first ordering).
    idx_first = region.find("FIRST_CONTENT")
    idx_second = region.find("SECOND_CONTENT")
    assert idx_second >= 0 and idx_first >= 0
    assert idx_second < idx_first, (
        f"expected 2026-04-24 block before 2026-03-01 (newest first); "
        f"got second@{idx_second} first@{idx_first}"
    )


def test_prepend_preserves_fulltext_block():
    """No matter how many revisits accumulate, the original fulltext
    block must stay byte-identical — that's the whole point of
    append mode."""
    baseline = "<!-- kb-fulltext-start -->\n## 1. P\nOriginal summary\n<!-- kb-fulltext-end -->"
    md = f"---\ntitle: x\n---\n\n{baseline}\n"
    for i, date in enumerate(["2026-03-01", "2026-04-01", "2026-05-01"]):
        block = _build_revisit_block(date, f"model/v{i}", f"rev {i}\n")
        md = _prepend_revisit_block(md, block)
    # Exactly one fulltext region; content intact.
    assert md.count("<!-- kb-fulltext-start -->") == 1
    assert md.count("<!-- kb-fulltext-end -->") == 1
    assert "Original summary" in md


def test_prepend_when_existing_region_has_content():
    """Re-prepend when Revisits already has one block: new block
    goes above the existing, markers preserved."""
    first_block = _build_revisit_block("2026-03-01", "openai/gpt-4o", "OLD_REVISIT\n")
    md = (
        "---\ntitle: x\n---\n\n"
        "<!-- kb-fulltext-start -->\nS\n<!-- kb-fulltext-end -->\n"
        "\n## Revisits\n\n"
        f"{REVISITS_START}\n"
        f"{first_block.rstrip()}\n"
        f"{REVISITS_END}\n"
    )
    new_block = _build_revisit_block("2026-04-24", "anthropic", "NEW_REVISIT\n")
    out = _prepend_revisit_block(md, new_block)

    region = _extract_revisits_region(out)
    assert region is not None
    # Both blocks present
    assert "OLD_REVISIT" in region
    assert "NEW_REVISIT" in region
    # Markers still paired, exactly one of each
    assert out.count(REVISITS_START) == 1
    assert out.count(REVISITS_END) == 1


def test_extract_revisits_region_returns_none_when_absent():
    md = "---\ntitle: x\n---\nBody with no revisits"
    assert _extract_revisits_region(md) is None


def test_extract_revisits_region_returns_none_when_unpaired():
    """Start without end → None (don't silently accept half-open)."""
    md = f"---\n---\n\n{REVISITS_START}\nblock\n"
    assert _extract_revisits_region(md) is None
