"""Regression for the v0.27.5 field-report classifier audit.

re-summarize's classifier was armored in 0.27.1 (new
skip_bad_target category, `.code` attribute preference,
broader substring fallback) but re-read's sibling classifier
was not updated at the same time. A batch that hit a stale
paper_key (md already deleted) or a paper with no
zotero_attachment_keys landed the event in skip_llm_error,
overcounting "LLM failures" in the daily kb-mcp report.

0.27.6 brings re-read into parity:
  - new RE_READ_SKIP_BAD_TARGET category
  - code "no_attachment_keys"      → skip_pdf_missing
  - code in bad_target/md_not_found/paper_not_found
                                   → skip_bad_target
  - substring "paper md not found" → skip_bad_target
    (checked BEFORE the llm-fallback)
  - substring "cannot locate"/"no zotero_attachment_keys"
                                   → skip_pdf_missing

These tests invoke the classifier indirectly by driving
`re_read()` through the ReSummarizeError path with a
stub `re_summarize` so no LLM is called and we can assert
the category landing in `events.jsonl`."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


class _FakeReSummarizeError(Exception):
    """Stand-in for the real ReSummarizeError that lives in
    kb_write.ops.re_summarize. Matches the interface the re_read
    classifier cares about: str(e) and optional `.code`."""
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _run_one_with_error(tmp_path: Path, err: Exception, monkeypatch):
    """Run a minimal re_read() invocation where `re_summarize` raises
    `err`. Capture the event category that was recorded."""
    from kb_write.ops import re_read as rr
    from kb_write.config import WriteContext

    # Stub the real re_summarize + ReSummarizeError that re_read imports.
    import kb_write.ops.re_summarize as rs
    monkeypatch.setattr(
        rs, "ReSummarizeError", _FakeReSummarizeError,
        raising=False,
    )
    def _boom(ctx, target, *a, **kw):
        raise err
    monkeypatch.setattr(rs, "re_summarize", _boom, raising=True)

    # Stub the selector + source so we get exactly one key.
    # Easiest: drive re_read() with a trivial source pool that has one paper.
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    (papers_dir / "TESTPK01.md").write_text(
        "---\nkind: paper\ntitle: t\nfulltext_processed: true\n---\n"
    )

    ctx = WriteContext(
        kb_root=tmp_path,
        git_commit=False, reindex=False, lock=False, dry_run=False,
    )

    # Capture events that get recorded.
    captured: list[tuple[str, str]] = []
    import kb_importer.events as ev
    orig_record = ev.record_event
    def _capture(kb_root, *, event_type, paper_key, category, **kw):
        captured.append((event_type, category))
    monkeypatch.setattr(ev, "record_event", _capture, raising=True)

    # Use source=papers and selector=random with count=1, seed for
    # determinism.
    rr.re_read(
        ctx, count=1, source_name="papers", selector_name="random",
        seed=42, dry_run=False,
    )
    monkeypatch.setattr(ev, "record_event", orig_record, raising=True)
    return captured


class TestCodeFirst:
    """Code attribute takes priority over message substring."""

    def test_code_no_attachment_keys_routes_to_pdf(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError(
            "XYZ: no zotero_attachment_keys in frontmatter — cannot "
            "locate the PDF.",
            code="no_attachment_keys",
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_pdf_missing") in events, events

    def test_code_bad_target_routes_to_bad_target(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError(
            "paper md not found: papers/GHOSTKEY.md",
            code="bad_target",
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_bad_target") in events, events

    def test_code_md_not_found_routes_to_bad_target(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError(
            "paper md not found: papers/GHOST.md",
            code="md_not_found",
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_bad_target") in events, events

    def test_code_pdf_missing_routes_to_pdf(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError("PDF missing", code="pdf_missing")
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_pdf_missing") in events, events

    def test_code_not_processed_routes_to_not_processed(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError("fulltext_processed is not true",
                                    code="not_processed")
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_not_processed") in events, events

    def test_code_mtime_routes_to_mtime(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError("mtime changed mid-run",
                                    code="mtime_conflict")
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_mtime_conflict") in events, events

    def test_code_bad_request_routes_to_llm(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError("HTTP 400 invalid prompt",
                                    code="bad_request")
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_llm_error") in events, events


class TestSubstringFallback:
    """When no `.code` is set, substring matching must route to
    the right bucket. These assertions lock the v0.27.6 broadened
    substrings — the gap 0.27.5's re-summarize review shipped with."""

    def test_paper_md_not_found_substring_to_bad_target(self, tmp_path, monkeypatch):
        """The v0.27.1 re-summarize fix added a bad-target substring
        check BEFORE the llm fallback. re_read needs the same ordering
        — otherwise typo'd keys end up in skip_llm_error."""
        err = _FakeReSummarizeError(
            "paper md not found: papers/GHOSTKEY.md",
            # code=None → fall back to substring
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_bad_target") in events, events

    def test_no_zotero_attachment_keys_substring_to_pdf(
        self, tmp_path, monkeypatch,
    ):
        """'no zotero_attachment_keys' is the exact v26.5 field
        observation; it's a pdf-locate failure, not an llm failure."""
        err = _FakeReSummarizeError(
            "XYZ: no zotero_attachment_keys in frontmatter — "
            "cannot locate the PDF.",
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_pdf_missing") in events, events

    def test_cannot_locate_substring_to_pdf(self, tmp_path, monkeypatch):
        """Alternate phrasing: an LLM that responds 'I cannot locate
        the PDF content' would previously hit skip_llm_error by
        fallthrough; now routes to skip_pdf_missing."""
        err = _FakeReSummarizeError(
            "cannot locate PDF content for this paper",
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_pdf_missing") in events, events

    def test_not_processed_substring_to_not_processed(
        self, tmp_path, monkeypatch,
    ):
        err = _FakeReSummarizeError(
            "fulltext_processed is not true for this paper"
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_not_processed") in events, events

    def test_mtime_substring_to_mtime(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError(
            "paper md changed mid-run — mtime moved forward"
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_mtime_conflict") in events, events

    def test_generic_llm_error_falls_through_to_llm(
        self, tmp_path, monkeypatch,
    ):
        """A genuinely-LLM error that matches none of the other
        substrings should still land in skip_llm_error — otherwise
        the broader fallback would swallow real LLM failures."""
        err = _FakeReSummarizeError("HTTP 500 upstream provider error")
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_llm_error") in events, events


class TestUnknownCode:
    """Forward-compat: an exception with a `.code` the classifier
    doesn't recognise must still produce a category, and must NOT
    silently slip through substring fallback (otherwise a new code
    with a misleading message could go to a wrong bucket). v0.27.6
    keeps the existing "unknown code → skip_llm_error" default."""

    def test_unknown_code_defaults_to_llm(self, tmp_path, monkeypatch):
        err = _FakeReSummarizeError(
            "a brand new failure mode not yet in the classifier",
            code="future_code_v99",
        )
        events = _run_one_with_error(tmp_path, err, monkeypatch)
        assert ("re_read", "skip_llm_error") in events, events


class TestSelectorCompat:
    """unread-first selector must recognise the new SKIP_BAD_TARGET
    category as 'this paper was attempted' so a subsequent run
    doesn't keep re-picking the same stale key forever. This test
    proves the selector's executed_cats set was widened alongside
    the classifier."""

    def test_bad_target_marks_paper_as_read(self, tmp_path):
        # Seed an events.jsonl with a SKIP_BAD_TARGET entry.
        kb_root = tmp_path
        (kb_root / ".kb-mcp").mkdir()
        from kb_importer.events import (
            record_event, EVENT_RE_READ, RE_READ_SKIP_BAD_TARGET,
        )
        record_event(
            kb_root,
            event_type=EVENT_RE_READ,
            paper_key="GHOSTKEY",
            category=RE_READ_SKIP_BAD_TARGET,
            detail="md not found",
            pipeline="re_read",
        )

        from kb_write.selectors.unread_first import _load_read_set
        read = _load_read_set(kb_root)
        assert "GHOSTKEY" in read, (
            "skip_bad_target events should count as 'attempted' in "
            "unread-first's read-set — otherwise selection will keep "
            "returning GHOSTKEY forever, re-failing the same way."
        )
