"""Regression for v26.5 bug #7: re-summarize failure classifier
landed too many errors in `skip_llm_error` because its
substring match was narrow. v27 fixes both the classifier (more
substring patterns + new bad_target code) AND adds `code=`
attributes to the relevant raise sites so the classifier doesn't
depend on substring matching for well-known failure modes.

Tests exercise the classifier directly with representative
exception shapes from the field report."""
from __future__ import annotations

import pytest


def _classify(err, paper_target="PK1"):
    """Invoke the classifier and return the event category that
    was recorded. Monkey-patch record_event to capture the call."""
    from kb_write.ops import re_summarize as rs
    captured = {}

    def _fake_record(kb_root, *, event_type, paper_key, category, **kw):
        captured["event_type"] = event_type
        captured["category"] = category

    import kb_importer.events as ev
    orig = ev.record_event
    ev.record_event = _fake_record
    try:
        rs._record_re_summarize_failure(
            kb_root=None, paper_target=paper_target, err=err,
            provider="test", model_tried="test",
        )
    finally:
        ev.record_event = orig
    return captured.get("category")


class TestCodeFirstClassification:
    """When the exception carries a `.code` attribute, classification
    must use it — substring matching is only a fallback."""

    def test_md_not_found_code(self):
        from kb_write.ops.re_summarize import ReSummarizeError
        # The v26.5 field report case: "NOT_A_REAL_KEY: paper md
        # not found: papers/NOT_A_REAL_KEY.md". Previously landed
        # in skip_llm_error.
        e = ReSummarizeError(
            "paper md not found: papers/NOT_A_REAL_KEY.md",
            code="md_not_found",
        )
        assert _classify(e) == "skip_bad_target"

    def test_bad_target_code(self):
        from kb_write.ops.re_summarize import ReSummarizeError
        e = ReSummarizeError(
            "PK1: md has no frontmatter block",
            code="bad_target",
        )
        assert _classify(e) == "skip_bad_target"

    def test_pdf_missing_code_routes_to_pdf_bucket(self):
        from kb_write.ops.re_summarize import ReSummarizeError
        # The v26.5 field report case #2: "5N6FQXJJ: no
        # zotero_attachment_keys in frontmatter — cannot locate
        # the PDF". Must NOT land in skip_llm_error.
        e = ReSummarizeError(
            "5N6FQXJJ: PDF missing — no zotero_attachment_keys",
            code="pdf_missing",
        )
        assert _classify(e) == "skip_pdf_missing"

    def test_no_attachment_keys_code_routes_to_pdf_bucket(self):
        """Alternative code that also means 'can't locate PDF'."""
        from kb_write.ops.re_summarize import ReSummarizeError
        e = ReSummarizeError(
            "foo: adapter failed", code="no_attachment_keys",
        )
        assert _classify(e) == "skip_pdf_missing"

    def test_bad_request_code_routes_to_llm(self):
        from kb_write.ops.re_summarize import ReSummarizeError
        e = ReSummarizeError("LLM refused", code="bad_request")
        assert _classify(e) == "skip_llm_error"

    def test_mtime_conflict_code(self):
        from kb_write.ops.re_summarize import ReSummarizeError
        e = ReSummarizeError("md changed", code="mtime_conflict")
        assert _classify(e) == "skip_mtime_conflict"

    def test_not_processed_code(self):
        from kb_write.ops.re_summarize import ReSummarizeError
        e = ReSummarizeError("no initial summary", code="not_processed")
        assert _classify(e) == "skip_not_processed"


class TestSubstringFallback:
    """Old exceptions without `.code` must still classify correctly.
    These tests are the belt-and-suspenders layer: even if a future
    raise site forgets to pass code=, the message text should drive
    the right bucket."""

    def test_paper_md_not_found_by_message(self):
        e = Exception("paper md not found: papers/BAD.md")
        assert _classify(e) == "skip_bad_target"

    def test_cannot_locate_pdf_by_message(self):
        """v26.5 field report exact wording — classifier v26
        matched only ("pdf" + "missing"/"not found"/"no pdf"),
        missing this phrasing. v27 adds "cannot locate"."""
        e = Exception(
            "5N6FQXJJ: no zotero_attachment_keys in frontmatter — "
            "cannot locate the PDF."
        )
        got = _classify(e)
        assert got == "skip_pdf_missing", (
            f"v26.5 regression: message with 'cannot locate the PDF' "
            f"landed in {got!r} instead of skip_pdf_missing"
        )

    def test_no_zotero_attachment_keys_by_message(self):
        """Even without the word 'PDF' at all, a message mentioning
        the attachment-keys frontmatter key is a PDF-locate
        failure, not an LLM failure."""
        e = Exception("foo: no zotero_attachment_keys in frontmatter")
        got = _classify(e)
        assert got == "skip_pdf_missing", got

    def test_plain_llm_failure_still_routes_to_llm(self):
        """Negative control: a generic LLM error should still land
        in skip_llm_error even though we broadened PDF matching."""
        e = Exception("OpenAI returned 500 internal server error")
        assert _classify(e) == "skip_llm_error"
