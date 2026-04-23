"""Regression for the v0.27.10 stale-row cleanup.

Pre-0.27.10: if a md on disk was previously indexed as one node
type, then its frontmatter was edited so the indexer no longer
recognises it (kind mismatch, YAML corrupted, etc.), the
indexer just logged + returned. The old DB row (+ paper_fts,
paper_chunk_meta, links) stayed until the md file itself was
deleted. Search/backlinks/graph kept returning a phantom node.

v0.27.10 adds `_delete_stale_node_row()` and wires it into both
the kind-mismatch branch and the outer `except Exception`
handler of every `_index_*` method. When a row existed for a
file that's no longer a valid node of that type, the row (and
all its side-table entries) is removed."""
from __future__ import annotations

from conftest import skip_if_no_frontmatter, skip_if_no_mcp


def _make_indexer(tmp_path):
    from kb_mcp.store import Store
    from kb_mcp.indexer import Indexer

    store = Store(tmp_path / "index.sqlite")
    store.ensure_schema()
    idx = Indexer(tmp_path, store, embedding_provider=None)
    return idx, store


def test_kind_mismatch_removes_existing_paper_row(tmp_path):
    """Scenario: papers/P1.md existed with kind=paper. User edits
    YAML so kind becomes something else. Reindex must DELETE the
    stale papers row so search/backlinks don't keep returning
    it."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()

    idx, store = _make_indexer(tmp_path)
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    md = papers_dir / "P1.md"
    # Seed DB row directly (pretend previous reindex had it).
    store.execute(
        "INSERT INTO papers "
        "(paper_key, zotero_key, title, md_path, md_mtime, "
        "last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("P1", "P1", "paper one", "papers/P1.md", 1.0, "x"),
    )
    store.execute(
        "INSERT INTO paper_fts (paper_key, title, authors, "
        "abstract, fulltext) VALUES (?, ?, ?, ?, ?)",
        ("P1", "paper one", "a", "abstract", "ft"),
    )
    store.commit()

    # Verify row present.
    assert store.execute(
        "SELECT 1 FROM papers WHERE paper_key = ?", ("P1",)
    ).fetchone() is not None

    # Write md with kind=note (mismatch for a paper-bucket file).
    md.write_text(
        "---\nkind: note\ntitle: hijacked\n---\n\nbody\n"
    )

    from kb_mcp.indexer import IndexReport
    report = IndexReport()
    idx._index_paper(md, report)

    # Row MUST be gone.
    assert store.execute(
        "SELECT 1 FROM papers WHERE paper_key = ?", ("P1",)
    ).fetchone() is None, (
        "kind-mismatch in md left papers row behind — "
        "phantom paper would keep showing in search/backlinks"
    )
    # paper_fts side-table must also be gone.
    assert store.execute(
        "SELECT 1 FROM paper_fts WHERE paper_key = ?", ("P1",)
    ).fetchone() is None


def test_frontmatter_parse_failure_removes_existing_paper_row(tmp_path):
    """If frontmatter.load raises (e.g. malformed YAML), the
    outer exception handler in _index_papers should clean the
    stale row in addition to recording the error."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()

    idx, store = _make_indexer(tmp_path)
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    md = papers_dir / "P2.md"

    # Seed DB row.
    store.execute(
        "INSERT INTO papers "
        "(paper_key, zotero_key, md_path, md_mtime, "
        "last_indexed_at) VALUES (?, ?, ?, ?, ?)",
        ("P2", "P2", "papers/P2.md", 1.0, "x"),
    )
    store.commit()

    # Write md with malformed YAML that frontmatter will reject.
    md.write_text(
        "---\n"
        "title: [this is not valid yaml because: the bracket never closes\n"
        "---\nbody\n"
    )

    from kb_mcp.indexer import IndexReport
    report = IndexReport()
    idx._index_papers(report)

    # Row MUST be gone.
    assert store.execute(
        "SELECT 1 FROM papers WHERE paper_key = ?", ("P2",)
    ).fetchone() is None, (
        "frontmatter parse error left papers row behind"
    )
    # The parse error itself should still be surfaced in the report.
    assert any(
        "P2" in e[0] for e in report.errors
    ), f"parse error not in report.errors: {report.errors}"


def test_kind_mismatch_on_fresh_file_does_nothing(tmp_path):
    """If no prior row existed, kind mismatch is just a skip.
    The cleanup helper must be a no-op in that case (not crash)."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()

    idx, store = _make_indexer(tmp_path)
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    md = papers_dir / "FRESH.md"
    md.write_text("---\nkind: thought\ntitle: t\n---\nbody\n")

    from kb_mcp.indexer import IndexReport
    report = IndexReport()
    idx._index_paper(md, report)

    # No exception; no row ever existed; no row now.
    assert store.execute(
        "SELECT 1 FROM papers WHERE paper_key = ?", ("FRESH",)
    ).fetchone() is None


def test_delete_stale_node_row_idempotent_missing_key(tmp_path):
    """Calling the helper for a key that isn't in the table is
    a no-op (returns False, doesn't raise)."""
    skip_if_no_mcp()
    idx, store = _make_indexer(tmp_path)
    assert idx._delete_stale_node_row("papers", "DOES_NOT_EXIST") is False
    assert idx._delete_stale_node_row("notes", "DOES_NOT_EXIST") is False
    assert idx._delete_stale_node_row("topics", "DOES_NOT_EXIST") is False
    assert idx._delete_stale_node_row("thoughts", "DOES_NOT_EXIST") is False


def test_delete_stale_node_row_scrubs_paper_side_tables(tmp_path):
    """For papers, the helper must clean paper_chunk_meta and
    paper_fts too (they don't have cross-table FK in all code
    paths we run)."""
    skip_if_no_mcp()
    idx, store = _make_indexer(tmp_path)
    store.execute(
        "INSERT INTO papers "
        "(paper_key, zotero_key, md_path, md_mtime, "
        "last_indexed_at) VALUES (?, ?, ?, ?, ?)",
        ("PX", "PX", "papers/PX.md", 1.0, "x"),
    )
    store.execute(
        "INSERT INTO paper_chunk_meta "
        "(paper_key, kind, section_num, section_title, text) "
        "VALUES (?, ?, ?, ?, ?)",
        ("PX", "section", 1, "§1", "some chunk text"),
    )
    store.execute(
        "INSERT INTO paper_fts (paper_key, title, authors, "
        "abstract, fulltext) VALUES (?, ?, ?, ?, ?)",
        ("PX", "t", "a", "abs", "ft"),
    )
    store.commit()

    assert idx._delete_stale_node_row("papers", "PX") is True

    assert store.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE paper_key = ?",
        ("PX",),
    ).fetchone()["n"] == 0
    assert store.execute(
        "SELECT COUNT(*) AS n FROM paper_chunk_meta "
        "WHERE paper_key = ?", ("PX",),
    ).fetchone()["n"] == 0
    assert store.execute(
        "SELECT COUNT(*) AS n FROM paper_fts WHERE paper_key = ?",
        ("PX",),
    ).fetchone()["n"] == 0
