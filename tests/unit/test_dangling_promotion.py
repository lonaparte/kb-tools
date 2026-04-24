"""Regression for the v0.28.2 dangling-edge auto-promotion.

Stress-run finding G18: link_resolve.py's docstring claimed
"kb-mcp index again will automatically promote dangling edges
to real ones if the missing target got added" — but before 0.28.2
this only happened when the SRC md's mtime advanced, because the
incremental indexer only re-stages refs for touched srcs. A user
who imported paper B after A pointed to it with a dangling edge
had to ALSO touch A to see the edge promote. Surprising.

v0.28.2 adds _promote_dangling_edges, which runs on every index
pass, iterates the links table for dst_type='dangling', and
re-resolves each against the current node tables.

This test reproduces the exact scenario:
  1. Create thought A with kb_refs = [papers/B] (B doesn't exist).
  2. Index. Edge: thought/A → dangling/B.
  3. Create paper B (no touch on A).
  4. Index again.
  5. Edge should now be: thought/A → paper/B. (Pre-0.28.2: still dangling.)
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from conftest import skip_if_no_frontmatter, skip_if_no_mcp


def _write(path: pathlib.Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _links(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT src_type, src_key, dst_type, dst_key, origin FROM links"
    ).fetchall()]


def test_dangling_promotes_after_target_lands(tmp_path):
    skip_if_no_mcp()
    skip_if_no_frontmatter()
    from kb_mcp.store import Store, default_db_path
    from kb_mcp.indexer import Indexer

    kb = tmp_path
    _write(kb / "thoughts" / "2026-04-24-forward.md", (
        "---\n"
        "kind: thought\n"
        "title: forward\n"
        "kb_refs: [papers/LATECOMER]\n"
        "---\n"
        "body\n"
    ))

    # --- Step 1+2: target missing → edge is dangling.
    store = Store(default_db_path(kb))
    store.ensure_schema()
    r1 = Indexer(kb, store).index_all()
    store.close() if hasattr(store, "close") else None

    edges = _links(default_db_path(kb))
    assert any(e["dst_type"] == "dangling" and e["dst_key"] == "LATECOMER"
               for e in edges), f"expected dangling edge, got: {edges}"

    # --- Step 3: target lands. Note: A's mtime does NOT advance.
    _write(kb / "papers" / "LATECOMER.md", (
        "---\n"
        "kind: paper\n"
        "title: late\n"
        "zotero_key: LATECOMER\n"
        "item_type: journalArticle\n"
        "---\n"
        "body\n"
        "<!-- kb-ai-zone-start -->\n"
        "<!-- kb-ai-zone-end -->\n"
    ))

    # --- Step 4: incremental index.
    store2 = Store(default_db_path(kb))
    store2.ensure_schema()
    r2 = Indexer(kb, store2).index_all()

    # --- Step 5: edge promoted.
    edges_after = _links(default_db_path(kb))
    dangling = [e for e in edges_after if e["dst_type"] == "dangling"]
    real = [e for e in edges_after if
            e["dst_type"] == "paper" and e["dst_key"] == "LATECOMER"]

    assert real, (
        f"expected promoted thought→paper edge, got: {edges_after}"
    )
    assert not dangling, (
        f"expected 0 dangling after promotion, got {len(dangling)}: "
        f"{dangling}"
    )
    # IndexReport should surface the promotion count.
    assert getattr(r2, "links_promoted", 0) >= 1, (
        f"expected links_promoted >= 1, got {getattr(r2, 'links_promoted', 'missing')}"
    )


def test_promotion_is_idempotent(tmp_path):
    """Running index twice after everything is already resolved
    shouldn't spuriously re-promote or churn the links table."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()
    from kb_mcp.store import Store, default_db_path
    from kb_mcp.indexer import Indexer

    kb = tmp_path
    _write(kb / "papers" / "ABCD1234.md", (
        "---\nkind: paper\ntitle: target\nzotero_key: ABCD1234\n"
        "item_type: journalArticle\n---\nbody\n"
        "<!-- kb-ai-zone-start -->\n<!-- kb-ai-zone-end -->\n"
    ))
    _write(kb / "thoughts" / "2026-04-24-refs.md", (
        "---\nkind: thought\ntitle: refs\n"
        "kb_refs: [papers/ABCD1234]\n---\nbody\n"
    ))

    store = Store(default_db_path(kb)); store.ensure_schema()
    Indexer(kb, store).index_all()
    edges_a = _links(default_db_path(kb))

    # No mtime changes, nothing new — should be a no-op.
    r2 = Indexer(kb, store).index_all()
    edges_b = _links(default_db_path(kb))

    assert edges_a == edges_b, "idle re-index changed link edges"
    assert r2.links_promoted == 0
