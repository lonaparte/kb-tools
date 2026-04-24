"""Doctor must flag malformed revisits regions.

Invariant: if any of kb-revisits-start / kb-revisits-end /
kb-revisit-block markers appears, they must balance and pair
correctly. Half-open regions usually indicate a hand-edited file
or a crashed re-summarize — surface as errors so the user repairs
before the indexer ingests broken boundaries.
"""
from __future__ import annotations

from pathlib import Path

from kb_write.ops.doctor import _check_revisits_markers, DoctorReport


def _scaffold_paper(tmp_path: Path, body: str) -> Path:
    """Write a minimal papers/X.md under tmp_path (which acts as
    kb_root). Returns the kb_root."""
    papers = tmp_path / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    md = papers / "ABCD1234.md"
    md.write_text(
        f"---\nkind: paper\nzotero_key: ABCD1234\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_no_revisits_markers_is_silent(tmp_path):
    """A paper with no revisits region produces no findings."""
    kb_root = _scaffold_paper(tmp_path, "## Abstract\nfoo\n")
    report = DoctorReport()
    _check_revisits_markers(kb_root, report)
    assert report.findings == []


def test_well_formed_region_is_silent(tmp_path):
    body = (
        "## Abstract\nfoo\n\n"
        "## Revisits\n\n"
        "<!-- kb-revisits-start -->\n"
        "<!-- kb-revisit-block date=\"2026-04-24\" model=\"x\" -->\n"
        "### 2026-04-24 — x\nbody\n"
        "<!-- /kb-revisit-block -->\n"
        "<!-- kb-revisits-end -->\n"
    )
    kb_root = _scaffold_paper(tmp_path, body)
    report = DoctorReport()
    _check_revisits_markers(kb_root, report)
    assert report.findings == []


def test_start_without_end_errors(tmp_path):
    body = (
        "## Revisits\n\n"
        "<!-- kb-revisits-start -->\n"
        "<!-- kb-revisit-block date=\"2026-04-24\" model=\"x\" -->\n"
        "body\n"
        "<!-- /kb-revisit-block -->\n"
        # missing kb-revisits-end
    )
    kb_root = _scaffold_paper(tmp_path, body)
    report = DoctorReport()
    _check_revisits_markers(kb_root, report)
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.severity == "error"
    assert f.category == "revisits"
    assert "unbalanced" in f.message


def test_unbalanced_block_open_close_errors(tmp_path):
    body = (
        "<!-- kb-revisits-start -->\n"
        "<!-- kb-revisit-block date=\"2026-04-24\" model=\"x\" -->\n"
        "body\n"
        # missing block close
        "<!-- kb-revisit-block date=\"2026-05-01\" model=\"y\" -->\n"
        "body2\n"
        "<!-- /kb-revisit-block -->\n"
        "<!-- kb-revisits-end -->\n"
    )
    kb_root = _scaffold_paper(tmp_path, body)
    report = DoctorReport()
    _check_revisits_markers(kb_root, report)
    assert any(
        f.category == "revisits" and "unbalanced" in f.message
        for f in report.findings
    )


def test_end_before_start_errors(tmp_path):
    body = (
        "<!-- kb-revisits-end -->\n"
        "stray content\n"
        "<!-- kb-revisits-start -->\n"
    )
    kb_root = _scaffold_paper(tmp_path, body)
    report = DoctorReport()
    _check_revisits_markers(kb_root, report)
    assert len(report.findings) == 1
    # Either path (unbalanced count or order swap) counts, depending
    # on the exact structure above this is detected as
    # "start appears AFTER end" → message varies. Just assert error.
    assert report.findings[0].severity == "error"
    assert report.findings[0].category == "revisits"


def test_multiple_start_markers_errors(tmp_path):
    body = (
        "<!-- kb-revisits-start -->\n"
        "<!-- kb-revisits-end -->\n"
        "<!-- kb-revisits-start -->\n"
        "second stray start\n"
        "<!-- kb-revisits-end -->\n"
    )
    kb_root = _scaffold_paper(tmp_path, body)
    report = DoctorReport()
    _check_revisits_markers(kb_root, report)
    assert any(
        "unbalanced" in f.message and f.category == "revisits"
        for f in report.findings
    )
