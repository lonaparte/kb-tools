"""Regression for the v0.27.10 refresh-counts total-papers mis-
statistic.

Pre-0.27.10 `count_papers()` was `SELECT COUNT(*) FROM papers`,
which in a library with book-chapter splits (one
`BOOKKEY.md` whole-work row plus N `BOOKKEY-chNN.md` chapter
rows, all sharing zotero_key=BOOKKEY) counted every chapter as
a separate paper. But `list_papers_with_doi()` filters to
whole-work rows only (`paper_key == zotero_key`), so a
`skipped_no_doi = total_papers - len(papers_with_doi)` line
over-counts by the chapter-count.

v0.27.10: count_papers() now filters the same way so the two
metrics are comparable."""
from __future__ import annotations

from conftest import skip_if_no_mcp


def test_count_papers_excludes_chapter_rows(tmp_path):
    skip_if_no_mcp()
    from kb_mcp.store import Store
    from kb_mcp.citation_ops import count_papers

    (tmp_path / ".kb-mcp").mkdir()
    db = tmp_path / ".kb-mcp" / "index.sqlite"
    store = Store(db)
    store.ensure_schema()

    now = "2026-04-23T00:00:00Z"
    # 3 whole-works.
    for pk in ("P1", "P2", "BOOK1"):
        store.execute(
            "INSERT INTO papers "
            "(paper_key, zotero_key, md_path, md_mtime, "
            "last_indexed_at) VALUES (?, ?, ?, ?, ?)",
            (pk, pk, f"papers/{pk}.md", 1.0, now),
        )
    # 2 book chapters (share zotero_key with BOOK1).
    for ch in (1, 2):
        chpk = f"BOOK1-ch{ch:02d}"
        store.execute(
            "INSERT INTO papers "
            "(paper_key, zotero_key, md_path, md_mtime, "
            "last_indexed_at) VALUES (?, ?, ?, ?, ?)",
            (chpk, "BOOK1", f"papers/{chpk}.md", 1.0, now),
        )
    store.commit()
    store.close()

    # Old semantics (the bug): 5 rows total. New semantics: 3.
    assert count_papers(tmp_path) == 3, (
        "count_papers must exclude chapter rows so it matches "
        "list_papers_with_doi's filter; the report's skipped_no_doi "
        "math relies on this."
    )


def test_count_papers_empty_library(tmp_path):
    skip_if_no_mcp()
    from kb_mcp.store import Store
    from kb_mcp.citation_ops import count_papers

    (tmp_path / ".kb-mcp").mkdir()
    store = Store(tmp_path / ".kb-mcp" / "index.sqlite")
    store.ensure_schema()
    store.close()
    assert count_papers(tmp_path) == 0


def test_count_papers_only_chapters_returns_zero(tmp_path):
    """Pathological: only chapter rows, no whole-work. Shouldn't
    really happen in practice (chapters are always created
    alongside a whole-work), but the semantics should still be
    consistent."""
    skip_if_no_mcp()
    from kb_mcp.store import Store
    from kb_mcp.citation_ops import count_papers

    (tmp_path / ".kb-mcp").mkdir()
    store = Store(tmp_path / ".kb-mcp" / "index.sqlite")
    store.ensure_schema()
    store.execute(
        "INSERT INTO papers "
        "(paper_key, zotero_key, md_path, md_mtime, "
        "last_indexed_at) VALUES (?, ?, ?, ?, ?)",
        ("BOOK1-ch01", "BOOK1", "papers/BOOK1-ch01.md",
         1.0, "2026-04-23T00:00:00Z"),
    )
    store.commit()
    store.close()

    # No whole-work row exists, so count should be 0.
    assert count_papers(tmp_path) == 0
