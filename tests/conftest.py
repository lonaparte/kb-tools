"""Pytest config.

Adds src/ paths to sys.path so tests can `import kb_core` / `import
kb_write` / etc. without requiring `pip install -e .` first. Mirrors
what scripts/test_e2e.py does — the two test paths should see the
same module tree.

Also exposes three shared skip-helpers for optional-dep guards.
The convention is: tests that transitively need `mcp`,
`python-frontmatter`, or `httpx` call the appropriate helper as
their first statement (after the docstring), so that stdlib-only
CI runs skip them cleanly rather than failing at collection time.
Historically each test file re-declared its own helper; 0.27.9
centralised them here so adding a new optional dep in the future
is a one-line addition rather than a copy-paste.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIRS = [
    REPO / "kb_core/src",
    REPO / "kb_write/src",
    REPO / "kb_mcp/src",
    REPO / "kb_importer/src",
    REPO / "kb_citations/src",
]

for p in _SRC_DIRS:
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Put `tests/` on sys.path so unit tests can `from conftest import
# skip_if_no_X`. Pytest loads conftest.py automatically; the
# stdlib-only `scripts/run_unit_tests.py` runner doesn't — so
# both runners need `tests/` on the path, and the runner also
# inserts it explicitly before collecting test files.
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------
# Shared optional-dep skip-guards
# ---------------------------------------------------------------------
# Invoke as the first body line of any test that transitively needs
# the named dep. Signature / behaviour is deliberately plain: no
# pytest fixtures, no markers — just a function that skips when the
# dep is missing. This keeps it callable from class methods + free
# functions + both `pytest` and the stdlib-only
# `scripts/run_unit_tests.py` runner (which accepts the same
# `pytest.skip` semantics).


def skip_if_no_mcp() -> None:
    """Skip when the `mcp` package is missing.

    `kb_mcp.server` hard-imports `from mcp.server.fastmcp import
    FastMCP` at module scope. Tests that touch `kb_mcp.server` are
    un-collectible without `mcp` installed. Splitting server.py
    into a protocol-free runtime layer + a mcp-protocol adapter is
    v0.28 file-split scope; until then the guard is the portable
    workaround.
    """
    try:
        import mcp.server.fastmcp  # noqa: F401
    except ImportError:
        pytest.skip(
            "mcp package not installed; kb_mcp.server imports "
            "FastMCP at module top — server-level tests require it"
        )


def skip_if_no_frontmatter() -> None:
    """Skip when `python-frontmatter` is missing.

    kb_write's atomic md-read/write path (`kb_write.frontmatter`),
    kb_importer's md_io + longform + fulltext_writeback, and
    kb_citations.resolver all hard-import `frontmatter` at module
    top. Tests that transitively reach any of these fail to import
    without the package. `python-frontmatter` is declared as a
    required dep in every pyproject that needs it, so the
    standard bundle install always has it — the guard is for
    stdlib-only CI runs.
    """
    try:
        import frontmatter  # noqa: F401
    except ImportError:
        pytest.skip(
            "python-frontmatter not installed; kb_write.frontmatter "
            "and kb_importer.md_io require it for YAML frontmatter "
            "parsing"
        )


def skip_if_no_pyzotero() -> None:
    """Skip when `pyzotero` is missing.

    kb_importer.zotero_reader hard-imports `from pyzotero import
    zotero` at module top, so anything that touches that module
    (including the 0.29 children-fetch and no-auto-archive tests)
    won't collect in a stdlib-only environment. Full venv has it
    via the kb_importer dep.
    """
    try:
        import pyzotero  # noqa: F401
    except ImportError:
        pytest.skip(
            "pyzotero not installed; kb_importer.zotero_reader "
            "imports it at module top"
        )


def skip_if_no_httpx() -> None:
    """Skip when `httpx` is missing.

    kb_citations uses httpx for all external-provider calls
    (Semantic Scholar, OpenAlex). Tests exercising kb_citations
    paths need it present.
    """
    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip(
            "httpx not installed; kb_citations transport layer "
            "requires it"
        )
