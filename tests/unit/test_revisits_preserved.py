"""kb_importer must preserve the `## Revisits` section across
re-imports / sync / --force-fulltext. Without this, a sync of a
paper that had revisits would silently drop them."""
from __future__ import annotations

from kb_importer.md_io import (
    PreservedContent,
    _extract_revisits_section,
    inject_preserved,
)


def test_extract_returns_none_when_no_revisits():
    body = "## Abstract\nfoo\n\n## Attachments\nbar\n"
    assert _extract_revisits_section(body) is None


def test_extract_captures_full_section_with_heading():
    body = (
        "## Abstract\nfoo\n\n"
        "<!-- kb-fulltext-start -->\nS\n<!-- kb-fulltext-end -->\n\n"
        "## Revisits\n\n"
        "<!-- kb-revisits-start -->\n"
        "<!-- kb-revisit-block date=\"2026-04-24\" model=\"x\" -->\n"
        "### 2026-04-24 — x\nbody\n"
        "<!-- /kb-revisit-block -->\n"
        "<!-- kb-revisits-end -->\n"
    )
    section = _extract_revisits_section(body)
    assert section is not None
    assert section.startswith("## Revisits")
    assert "<!-- kb-revisits-start -->" in section
    assert "<!-- kb-revisits-end -->" in section
    assert "body" in section
    # Nothing above `## Revisits` was dragged in
    assert "Abstract" not in section
    assert "fulltext" not in section.lower() or "fulltext-" not in section


def test_extract_returns_none_on_unpaired_markers():
    """Start without end: corrupt; let doctor flag it, don't silently
    round-trip half of it."""
    body = (
        "<!-- kb-revisits-start -->\n"
        "orphan content\n"
    )
    assert _extract_revisits_section(body) is None


def test_inject_preserved_round_trips_revisits():
    """A freshly-generated md body (no revisits in it — kb-importer
    doesn't emit them) gets the preserved revisits block appended
    verbatim."""
    fresh_body = (
        "## Abstract\nfoo\n\n"
        "<!-- kb-ai-zone-start -->\nzone\n<!-- kb-ai-zone-end -->\n\n"
        "<!-- kb-fulltext-start -->\nSummary\n<!-- kb-fulltext-end -->\n"
    )
    preserved_revisits = (
        "## Revisits\n\n"
        "<!-- kb-revisits-start -->\n"
        "<!-- kb-revisit-block date=\"2026-04-24\" model=\"m\" -->\n"
        "### 2026-04-24 — m\nrev-body\n"
        "<!-- /kb-revisit-block -->\n"
        "<!-- kb-revisits-end -->\n"
    )
    preserved = PreservedContent(
        ai_zone_body="\nzone\n",
        fulltext_body="\nSummary\n",
        revisits_section=preserved_revisits,
    )
    out = inject_preserved(fresh_body, preserved)
    # Revisits appended
    assert "<!-- kb-revisits-start -->" in out
    assert "<!-- kb-revisits-end -->" in out
    assert "rev-body" in out
    # Original fulltext + ai-zone preserved
    assert "Summary" in out
    assert "zone" in out
    # ai-zone came BEFORE revisits (order preserved)
    assert out.index("zone") < out.index("rev-body")


def test_inject_preserved_no_revisits_leaves_body_alone():
    """When preserved.revisits_section is None, body is unchanged
    relative to the no-revisits branch."""
    fresh_body = (
        "<!-- kb-ai-zone-start -->\nzone\n<!-- kb-ai-zone-end -->\n"
        "<!-- kb-fulltext-start -->\nSummary\n<!-- kb-fulltext-end -->\n"
    )
    preserved = PreservedContent(
        ai_zone_body="\nzone\n",
        fulltext_body="\nSummary\n",
        revisits_section=None,
    )
    out = inject_preserved(fresh_body, preserved)
    assert "kb-revisits" not in out
    assert "## Revisits" not in out
