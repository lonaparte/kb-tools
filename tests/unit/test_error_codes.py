"""Tests for the v27 structured error codes on re_summarize /
summarize exceptions. These replace the v26 substring-based
classification.

What this guards against: a well-meaning refactor in summarize.py
that renames BadRequestError or drops the `code` attribute would
silently revert classification to v26 behaviour."""
from __future__ import annotations

import pytest


def test_summarizer_base_code():
    from kb_importer.summarize import SummarizerError
    assert SummarizerError.code == "llm_other"


def test_bad_request_code():
    from kb_importer.summarize import BadRequestError
    assert BadRequestError.code == "bad_request"


def test_pdf_missing_code():
    from kb_importer.summarize import PdfMissingError
    assert PdfMissingError.code == "pdf_missing"


def test_quota_code():
    from kb_importer.summarize import QuotaExhaustedError
    assert QuotaExhaustedError.code == "quota"


def test_subclass_tree():
    # BadRequestError and PdfMissingError must inherit from
    # SummarizerError so existing `except SummarizerError:` still catches.
    from kb_importer.summarize import (
        SummarizerError, BadRequestError, PdfMissingError,
        QuotaExhaustedError,
    )
    assert issubclass(BadRequestError, SummarizerError)
    assert issubclass(PdfMissingError, SummarizerError)
    assert issubclass(QuotaExhaustedError, SummarizerError)


def test_resummarize_error_default_code():
    from kb_write.ops.re_summarize import ReSummarizeError
    e = ReSummarizeError("plain message")
    assert e.code == "llm_other"
    assert str(e) == "plain message"


def test_resummarize_error_explicit_code():
    from kb_write.ops.re_summarize import ReSummarizeError
    e = ReSummarizeError("pdf missing", code="pdf_missing")
    assert e.code == "pdf_missing"
    assert str(e) == "pdf missing"


def test_resummarize_error_valid_codes():
    """The code values used by the re_read / re_summarize
    classifiers. A typo here would silently route events to
    'llm_other'."""
    from kb_write.ops.re_summarize import ReSummarizeError
    for code in ("not_processed", "pdf_missing", "mtime_conflict",
                 "bad_request", "llm_other", "quota"):
        e = ReSummarizeError(f"x {code}", code=code)
        assert e.code == code
