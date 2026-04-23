"""Stale-row cleanup for the indexer.

Extracted from indexer.py in v0.28.0. Two concerns:

- remove_orphans: DB rows whose md_path no longer exists on disk.
- delete_stale_node_row: md exists but its frontmatter no longer
  says it's the kind of node the DB has it recorded as (kind
  mismatch / YAML corruption).

Free functions accepting the Indexer instance as first arg, since
all state lives on the Indexer (store, kb_root).
"""
from __future__ import annotations

import logging


log = logging.getLogger(__name__)


def delete_stale_node_row(indexer, table: str, key: str) -> bool:
    """Remove DB rows for a node whose md exists on disk but
    whose frontmatter is no longer valid for `table` (kind
    mismatch, YAML corrupt, field missing, etc.).

    Pre-v0.27.10 the indexer returned silently in this
    situation, leaving a stale row that would keep surfacing
    in search / backlinks / graph until the md file itself
    was deleted. v0.27.10 makes the indexer DELETE the
    stale row (plus all FK-cascading + FTS + chunk + link
    side-table entries) defensively.

    Returns True iff a row actually existed and was removed.
    """
    pk_col, src_type = indexer._NODE_TABLE_META[table]
    row = indexer.store.execute(
        f"SELECT 1 FROM {table} WHERE {pk_col} = ?", (key,),
    ).fetchone()
    if not row:
        return False

    # Paper-specific side tables: FK CASCADE takes paper_tags,
    # paper_collections, paper_attachments. We still have to
    # handle paper_chunk_meta (has FK but we don't always run
    # with PRAGMA foreign_keys=ON), paper_chunks_vec (vec0 has
    # no FK), and paper_fts (virtual table, no FK).
    if table == "papers":
        chunk_ids = [
            r["chunk_id"] for r in indexer.store.execute(
                "SELECT chunk_id FROM paper_chunk_meta "
                "WHERE paper_key = ?", (key,),
            ).fetchall()
        ]
        indexer.store.execute(
            "DELETE FROM paper_chunk_meta WHERE paper_key = ?",
            (key,),
        )
        if chunk_ids and indexer.store.vec_available:
            cid_ph = ",".join("?" * len(chunk_ids))
            indexer.store.execute(
                f"DELETE FROM paper_chunks_vec "
                f"WHERE chunk_id IN ({cid_ph})",
                tuple(chunk_ids),
            )
        indexer.store.execute(
            "DELETE FROM paper_fts WHERE paper_key = ?", (key,),
        )

    indexer.store.execute(
        f"DELETE FROM {table} WHERE {pk_col} = ?", (key,),
    )
    # Outbound links: gone; inbound links: reclassify as
    # dangling (matches remove_orphans semantics — a deleted
    # node might come back; edges from others pointing at it
    # are still meaningful metadata).
    indexer.store.execute(
        "DELETE FROM links WHERE src_type = ? AND src_key = ?",
        (src_type, key),
    )
    indexer.store.execute(
        "UPDATE links SET dst_type = 'dangling' "
        "WHERE dst_type = ? AND dst_key = ?",
        (src_type, key),
    )
    indexer.store.commit()
    log.info(
        "Cleaned stale DB row: %s(%s=%r) — md exists but "
        "frontmatter no longer identifies it as that node type.",
        table, pk_col, key,
    )
    return True


def remove_orphans(indexer, report) -> None:
    """Delete DB rows whose md_path no longer exists on disk."""
    # v26: papers PK is now `paper_key` (the md stem). The other
    # three tables still use their v25 PK (notes.zotero_key,
    # topics.slug, thoughts.slug) — their md-to-row mapping is
    # still 1:1.
    for table, pk_col in [
        ("papers", "paper_key"),
        ("notes", "zotero_key"),
        ("topics", "slug"),
        ("thoughts", "slug"),
    ]:
        rows = indexer.store.execute(
            f"SELECT {pk_col} AS pk, md_path FROM {table}"
        ).fetchall()
        removed_keys = []
        for r in rows:
            if not (indexer.kb_root / r["md_path"]).exists():
                removed_keys.append(r["pk"])
        if removed_keys:
            # ON DELETE CASCADE takes care of dependent rows for papers.
            placeholders = ",".join("?" * len(removed_keys))
            indexer.store.execute(
                f"DELETE FROM {table} WHERE {pk_col} IN ({placeholders})",
                tuple(removed_keys),
            )
            # FTS doesn't have FK; clean it too.
            if table == "papers":
                indexer.store.execute(
                    f"DELETE FROM paper_fts WHERE paper_key IN ({placeholders})",
                    tuple(removed_keys),
                )
                # Phase 2b: clean chunk meta + vec. chunk_meta has
                # FK ON DELETE CASCADE to papers, but we don't run
                # with PRAGMA foreign_keys=ON everywhere, so be
                # explicit. Vec0 doesn't support FK at all.
                chunk_ids = [
                    r["chunk_id"] for r in indexer.store.execute(
                        f"SELECT chunk_id FROM paper_chunk_meta "
                        f"WHERE paper_key IN ({placeholders})",
                        tuple(removed_keys),
                    ).fetchall()
                ]
                indexer.store.execute(
                    f"DELETE FROM paper_chunk_meta "
                    f"WHERE paper_key IN ({placeholders})",
                    tuple(removed_keys),
                )
                if chunk_ids and indexer.store.vec_available:
                    cid_placeholders = ",".join("?" * len(chunk_ids))
                    indexer.store.execute(
                        f"DELETE FROM paper_chunks_vec "
                        f"WHERE chunk_id IN ({cid_placeholders})",
                        tuple(chunk_ids),
                    )
            # Phase 2c: link-graph cleanup for any node type.
            # Map table → src_type used in links.src_type.
            src_type_for = {
                "papers": "paper", "notes": "note",
                "topics": "topic", "thoughts": "thought",
            }
            lt = src_type_for[table]
            indexer.store.execute(
                f"DELETE FROM links "
                f"WHERE src_type = ? AND src_key IN ({placeholders})",
                (lt, *removed_keys),
            )
            # Incoming edges become dangling rather than disappear:
            # a deleted paper might come back, and edges from other
            # nodes pointing at it are still meaningful metadata.
            indexer.store.execute(
                f"UPDATE links SET dst_type = 'dangling' "
                f"WHERE dst_type = ? AND dst_key IN ({placeholders})",
                (lt, *removed_keys),
            )
            indexer.store.commit()
            report.removed += len(removed_keys)
