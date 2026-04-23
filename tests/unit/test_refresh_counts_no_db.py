"""Regression for v25 UX bug: `kb-citations refresh-counts` on a
KB without projection DB raised a raw Python traceback.

v27 fix: same friendly error format as `link` — stderr message
pointing at `kb-mcp index`, exit code 2 (user-fixable error)."""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest


def _skip_if_no_httpx():
    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip("httpx not installed; kb_citations unavailable")


def test_refresh_counts_no_db_friendly_error(tmp_path, monkeypatch):
    _skip_if_no_httpx()
    # Clean KB with no projection DB.
    (tmp_path / ".kb-mcp").mkdir()

    # Pretend we have a provider configured so build_provider doesn't
    # bail early on missing creds.
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-dummy")

    from kb_citations.cli import main

    argv = [
        "refresh-counts",
        "--kb-root", str(tmp_path),
        "--provider", "semantic_scholar",
    ]

    err = io.StringIO()
    out = io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        rc = main(argv)
    combined = err.getvalue() + out.getvalue()

    # 1. Exit code 2 (user-fixable config error), not 1 (general
    #    failure) or 0 (silent success masking a crash).
    assert rc == 2, (
        f"expected rc=2, got rc={rc}\n"
        f"stderr/stdout: {combined!r}"
    )

    # 2. Output mentions the fix: running `kb-mcp index`.
    assert "kb-mcp index" in combined, (
        f"refresh-counts should point user at `kb-mcp index` — "
        f"UX regression from v25. Got:\n{combined}"
    )

    # 3. Output does NOT contain a raw Python traceback.
    assert "Traceback" not in combined, (
        f"v25 regression: raw Python traceback leaked to user:\n"
        f"{combined}"
    )
