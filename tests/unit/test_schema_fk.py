"""Regression for v26 bug #1: schema v6 foreign keys targeted
`papers(zotero_key)` which isn't a PK/UNIQUE column. SQLite
raised "foreign key mismatch" on every INSERT, making the
entire projection DB unusable. v27 bumps to schema v7 with FKs
pointing at `papers(paper_key)` (the actual PK)."""
from __future__ import annotations

import sqlite3

import pytest


def _insert_paper(conn, paper_key="ABCD1234", zotero_key=None):
    """Helper — satisfy all NOT NULL columns on papers."""
    conn.execute(
        "INSERT INTO papers "
        "(paper_key, zotero_key, title, md_path, md_mtime, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            paper_key, zotero_key or paper_key, "Example",
            f"papers/{paper_key}.md", 0.0, "2026-04-23T00:00:00Z",
        ),
    )


def test_schema_v7_inserts_without_fk_mismatch(tmp_path):
    """Apply the full schema, enable FK enforcement, and attempt
    the INSERT pattern the indexer uses. Must succeed.

    Reproduces the v26 #1 failure: with v6 DDL + PRAGMA
    foreign_keys=ON, inserting a paper + a paper_chunk_meta row
    raised `OperationalError: foreign key mismatch - ...`."""
    from kb_mcp.store import _load_schema_sql
    schema_sql = _load_schema_sql()

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    # Critical: enforce FKs. The v26 bug only surfaced with this
    # on. Without it, SQLite silently accepted the bad FK targets.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)

    _insert_paper(conn, "ABCD1234")

    # Each of the four side tables must accept a row referencing
    # the paper's paper_key. Under v6 this raised "foreign key
    # mismatch" because the DDL said REFERENCES papers(zotero_key)
    # but zotero_key isn't PK/UNIQUE.
    conn.execute(
        "INSERT INTO paper_attachments (attachment_key, paper_key) "
        "VALUES (?, ?)",
        ("ATT1", "ABCD1234"),
    )
    conn.execute(
        "INSERT INTO paper_tags (paper_key, tag, source) "
        "VALUES (?, ?, ?)",
        ("ABCD1234", "t1", "zotero"),
    )
    conn.execute(
        "INSERT INTO paper_collections (paper_key, collection_name) "
        "VALUES (?, ?)",
        ("ABCD1234", "Library"),
    )
    conn.execute(
        "INSERT INTO paper_chunk_meta "
        "(paper_key, kind, text) VALUES (?, ?, ?)",
        ("ABCD1234", "header", "dummy chunk"),
    )
    conn.commit()

    # Verify rows landed.
    cnt = conn.execute("SELECT COUNT(*) FROM paper_attachments").fetchone()[0]
    assert cnt == 1
    conn.close()


def test_schema_v7_cascade_delete(tmp_path):
    """ON DELETE CASCADE must actually fire — another check that
    the FK is wired correctly, since SQLite's behaviour on a
    malformed FK is "silently don't cascade"."""
    from kb_mcp.store import _load_schema_sql
    schema_sql = _load_schema_sql()

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)
    _insert_paper(conn, "PK1", "ZK1")
    conn.execute(
        "INSERT INTO paper_tags (paper_key, tag, source) VALUES (?, ?, ?)",
        ("PK1", "tag1", "zotero"),
    )
    # Delete the parent paper; tag should cascade away.
    conn.execute("DELETE FROM papers WHERE paper_key = ?", ("PK1",))
    remaining = conn.execute("SELECT COUNT(*) FROM paper_tags").fetchone()[0]
    conn.commit()
    assert remaining == 0, (
        "ON DELETE CASCADE didn't fire — FK target is wrong again"
    )
    conn.close()


def test_schema_v7_fk_to_nonexistent_parent_rejected(tmp_path):
    """Opposite direction: inserting a side-table row that
    references a non-existent paper_key must be REJECTED."""
    from kb_mcp.store import _load_schema_sql
    schema_sql = _load_schema_sql()

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO paper_tags (paper_key, tag, source) "
            "VALUES (?, ?, ?)",
            ("NONEXISTENT", "t", "zotero"),
        )
        conn.commit()
    conn.close()
