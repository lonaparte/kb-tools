"""Incremental indexer: walks the v26 KB layout and projects md
contents into the SQLite store.

v26 layout scanned (see kb_mcp.paths.ACTIVE_SUBDIRS):
  - papers/                      → kind=paper (incl. book chapters
                                    named `<KEY>-ch<NN>.md` sharing
                                    the parent's Zotero key)
  - topics/standalone-note/      → kind=note  (was zotero-notes/ in v25)
  - topics/agent-created/        → kind=topic (was topics/ top-level)
  - thoughts/                    → kind=thought

Legacy v25 paths (zotero-notes/, top-level topics/*.md) are NOT
scanned — they're reported as deprecated by index-status so the
user can reorganise. Content at old paths is not auto-migrated.

Core invariant: the index is DERIVED. The md files are the source of
truth. Any row in the DB that doesn't correspond to a current md file
gets deleted. Any md whose mtime has advanced past the DB's record
gets re-indexed.

Typical flow:
    store = Store(db_path); store.ensure_schema()
    idx = Indexer(kb_root, store)
    report = idx.index_all()
    # report.new + report.updated + report.unchanged + report.removed
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from .link_extractor import ExtractedRef, extract_refs
from .paths import (
    PAPERS_DIR, TOPICS_STANDALONE_DIR, TOPICS_AGENT_DIR, THOUGHTS_DIR,
    ACTIVE_SUBDIRS, is_book_chapter_filename,
)
from .store import Store

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Section markers used by kb-importer's paper md template.
# We read these to know where the fulltext region begins/ends.
# ---------------------------------------------------------------------

FULLTEXT_START = "<!-- kb-fulltext-start -->"
FULLTEXT_END = "<!-- kb-fulltext-end -->"


@dataclass
class IndexReport:
    """Summary of one index_all() run."""
    new: int = 0            # md existed, wasn't in DB → inserted
    updated: int = 0        # md existed, was stale in DB → rewritten
    unchanged: int = 0      # mtime match → skipped
    removed: int = 0        # was in DB, md file gone → deleted from DB
    errors: list[tuple[str, str]] = field(default_factory=list)

    # Phase 2b: embedding pass stats.
    embedded_papers: int = 0        # papers freshly embedded this run
    embedded_chunks: int = 0        # total chunk vectors written
    embed_api_calls: int = 0        # number of /embeddings HTTP requests
    embed_tokens: int = 0           # sum of prompt_tokens returned
    embed_failed: int = 0           # papers where embedding raised
    embed_skipped: int = 0          # papers skipped because no provider

    # Phase 2c: link-graph stats.
    links_written: int = 0          # total edges inserted this run
    links_dangling: int = 0         # edges whose dst didn't resolve

    def total_changed(self) -> int:
        return self.new + self.updated + self.removed


class Indexer:
    """Indexes papers / notes / topics / thoughts into the SQLite store.

    The Indexer is deliberately stateless across calls — every run starts
    from the current DB + current filesystem state. No caching, no
    journaling. If you Ctrl-C mid-index, the next run picks up where it
    left off because partial commits are safe (each md is indexed in a
    single transaction).

    Phase 2b: optional embedding_provider. When provided AND the
    store has the vec table available, papers are split into chunks
    and embedded. When absent or embedding fails for a paper, the
    paper's core row still writes successfully; its `embedded` flag
    stays 0 so a later run can pick it up.
    """

    def __init__(
        self,
        kb_root: Path,
        store: Store,
        embedding_provider=None,   # EmbeddingProvider | None
        embedding_batch_size: int = 100,
        only_keys: set[str] | None = None,
        path_glob: str | None = None,
    ):
        self.kb_root = kb_root
        self.store = store
        self._embedder = embedding_provider
        self._batch_size = embedding_batch_size
        # Subset mode: if either filter is set, we skip orphan removal
        # (scanning a subset can't tell what's truly orphaned) and only
        # process md files matching the filter. Used by:
        # - kb-mcp index --only-key A,B,C : subset by zotero_key
        # - kb-mcp index --filter 'papers/HR*' : subset by path glob
        # - kb-importer pilot runs: embed 50 papers, check quality,
        #   iterate without nuking the main index
        self._only_keys: set[str] | None = set(only_keys) if only_keys else None
        self._path_glob: str | None = path_glob
        self._subset_mode: bool = bool(self._only_keys or self._path_glob)
        # Track paper keys whose core row updated this run. After the
        # per-subdir passes complete, we batch-embed these in one big
        # pass to minimize API round-trips (rather than 1 call/paper).
        self._pending_embed: list[str] = []
        # Phase 2c: staged link edges. Each item is
        # (src_type, src_key, ExtractedRef). First-pass extraction
        # runs inside each _index_* method; second-pass resolution
        # (unknown → paper/note/topic/thought/dangling) runs after all
        # nodes are indexed so cross-type refs work. List order doesn't
        # matter — we dedupe on (src, dst, origin) at insert.
        self._staged_links: list[tuple[str, str, ExtractedRef]] = []
        # Track which src nodes had their links staged this run, so
        # _resolve_staged_links can clear ONLY those sources' old
        # links (leaving unchanged nodes' edges intact).
        self._touched_srcs: set[tuple[str, str]] = set()

    # -----------------------------------------------------------------
    # Public entry points
    # -----------------------------------------------------------------

    def index_all(self) -> IndexReport:
        """Full sweep: index each md subdir, then remove orphaned rows.

        Phase 2b: if an EmbeddingProvider is configured, a final
        embedding pass runs for any papers whose core row was
        (re-)written this call. Previously-embedded unchanged papers
        are skipped.

        Phase 2c: after all nodes are indexed, a link-resolution pass
        converts staged ExtractedRef candidates into resolved edges.
        This happens AFTER all subdirs because a frontmatter ref in
        a thought might point to a paper in papers/, and we need the
        paper's row to exist before we can resolve it.

        Subset mode: if `only_keys` or `path_glob` was passed to the
        constructor, we process only matching md files, skip orphan
        removal (which would otherwise think un-scanned rows are
        orphans), and scope the link-resolution pass to only the
        touched sources.
        """
        report = IndexReport()
        self._index_papers(report)
        self._index_notes(report)
        self._index_topics(report)
        self._index_thoughts(report)
        if not self._subset_mode:
            self._remove_orphans(report)
        # Batch embedding after all core rows are written.
        self._run_embedding_pass(report)
        # Resolve link graph last: needs all node tables populated.
        self._resolve_staged_links(report)
        return report

    def _should_process(self, md: Path) -> bool:
        """Decide whether this md file is in-scope for this run.

        Not in subset mode → always True.
        `only_keys` set → only md whose stem (paper/note/thought/topic
            slug) is in the set.
        `path_glob` set → only md whose kb-root-relative path matches
            the glob.
        Both set → must match either (union, not intersection).
        """
        if not self._subset_mode:
            return True
        stem = md.stem
        if self._only_keys and stem in self._only_keys:
            return True
        if self._path_glob:
            from fnmatch import fnmatch
            rel = md.relative_to(self.kb_root).as_posix()
            if fnmatch(rel, self._path_glob):
                return True
        return False

    def reindex_if_stale(self) -> IndexReport:
        """Fast check + targeted reindex. Use this from MCP tool calls
        to ensure the DB reflects recent md edits.

        Strategy: scan all md mtimes (fast; ~50ms for 1000 files),
        compare against DB's md_mtime column, re-index any that diverge.
        """
        # For Phase 2a this is the same as index_all — we always scan
        # everything and skip unchanged rows. The mtime comparison is
        # inside each _index_* method. Separated as a named entry point
        # because MCP tools should call THIS, not index_all, to signal
        # the intent ("refresh on read").
        return self.index_all()

    # -----------------------------------------------------------------
    # Per-subdir indexers
    # -----------------------------------------------------------------

    def _index_papers(self, report: IndexReport) -> None:
        d = self.kb_root / "papers"
        if not d.exists():
            return
        for md in sorted(d.glob("*.md")):
            if md.name.startswith("."):
                continue
            if not self._should_process(md):
                continue
            try:
                self._index_paper(md, report)
            except Exception as e:
                log.exception("failed indexing paper %s", md.name)
                report.errors.append((str(md.relative_to(self.kb_root)), str(e)))

    def _index_paper(self, md: Path, report: IndexReport) -> None:
        # v26: paper_key is the md filename stem. For most papers it
        # equals zotero_key. For book / long-article chapters named
        # `<KEY>-chNN.md`, paper_key keeps the full stem (e.g.
        # "BOOKKEY-ch03") while zotero_key is the parent "BOOKKEY",
        # so all mds sharing a Zotero item share a zotero_key.
        paper_key = md.stem
        chapter_info = is_book_chapter_filename(md.name)
        if chapter_info is not None:
            zotero_key_logical, _chapter_num = chapter_info
        else:
            zotero_key_logical = paper_key

        md_rel = md.relative_to(self.kb_root).as_posix()
        md_mtime = md.stat().st_mtime

        row = self.store.execute(
            "SELECT md_mtime, embedded FROM papers WHERE paper_key = ?",
            (paper_key,),
        ).fetchone()

        if row and abs(row["md_mtime"] - md_mtime) < 1e-6:
            # md unchanged. But: if this paper has embedded=0 AND an
            # embedder is now available, we should re-queue it for
            # embedding without rewriting the row. This covers the
            # case where a previous index run ran without a working
            # provider (API key missing, quota exceeded) and marked
            # papers embedded=0 — we don't want those papers stuck
            # permanently unembedded just because their md hasn't
            # changed since.
            if (
                row["embedded"] == 0
                and self._embedder is not None
                and self.store.vec_available
            ):
                self._pending_embed.append(paper_key)
            report.unchanged += 1
            return

        is_new = row is None

        # Read frontmatter + body.
        post = frontmatter.load(str(md))
        fm = post.metadata

        if fm.get("kind") != "paper":
            # Not a kb-importer-generated paper md. Surface at
            # warning so users who accidentally lose the `kind:
            # paper` field (e.g. YAML edit gone wrong) see the
            # skip instead of silently losing the paper from the
            # index. True YAML parse errors take a different code
            # path (frontmatter.load raises → outer except catches
            # → report.errors), so this branch is only for
            # structurally-valid-but-miskeyed mds.
            log.warning(
                "skipping %s: frontmatter kind=%r, expected 'paper'; "
                "paper will not be indexed",
                md_rel, fm.get("kind"),
            )
            return

        # v26: frontmatter may also override zotero_key (for synthetic
        # chapter mds the value there is authoritative). If present,
        # use it; else fall back to the filename-derived logical key.
        zotero_key_effective = fm.get("zotero_key") or zotero_key_logical

        authors_json = json.dumps(fm.get("authors") or [], ensure_ascii=False)
        fulltext_body = _extract_fulltext_body(post.content)
        abstract = _extract_abstract(post.content)

        now = _now_iso()

        # UPSERT on paper_key (the md stem — unique per md file).
        # zotero_key may be shared across rows (book + chapters); we
        # update it as a regular column. Citation-count columns are
        # preserved across the UPSERT so `kb-citations refresh-counts`
        # data isn't wiped on every re-index (v24 invariant).
        self.store.execute("""
            INSERT INTO papers (
                paper_key, zotero_key,
                title, authors, year, item_type,
                citation_key, doi, publication, abstract,
                fulltext_processed, fulltext_source,
                md_path, md_mtime, md_size, last_indexed_at
            ) VALUES (?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?,  ?, ?, ?, ?)
            ON CONFLICT(paper_key) DO UPDATE SET
                zotero_key = excluded.zotero_key,
                title = excluded.title,
                authors = excluded.authors,
                year = excluded.year,
                item_type = excluded.item_type,
                citation_key = excluded.citation_key,
                doi = excluded.doi,
                publication = excluded.publication,
                abstract = excluded.abstract,
                fulltext_processed = excluded.fulltext_processed,
                fulltext_source = excluded.fulltext_source,
                md_path = excluded.md_path,
                md_mtime = excluded.md_mtime,
                md_size = excluded.md_size,
                last_indexed_at = excluded.last_indexed_at
        """, (
            paper_key, zotero_key_effective,
            fm.get("title") or "",
            authors_json,
            _safe_int(fm.get("year")),
            fm.get("item_type") or "",
            fm.get("citation_key") or "",
            fm.get("doi") or "",
            fm.get("publication") or "",
            abstract,
            1 if fm.get("fulltext_processed") else 0,
            fm.get("fulltext_source"),
            md_rel, md_mtime, md.stat().st_size, now,
        ))

        # Clear and repopulate dependent tables for this paper.
        self._replace_paper_attachments(paper_key, fm)
        self._replace_paper_tags(paper_key, fm)
        self._replace_paper_collections(paper_key, fm)
        self._replace_paper_fts(paper_key, fm, abstract, fulltext_body)

        # Phase 2c: stage outgoing links from this paper for later
        # resolution. Papers usually have few kb_refs (it's the AI's
        # thoughts and topics that heavily link out), but wikilinks
        # in the AI zone do occur.
        self._stage_refs("paper", paper_key, fm, post.content)

        # Phase 2b: invalidate existing chunks for this paper. The paper
        # md changed, so old embeddings no longer match current text.
        # Mark embedded=0; actual chunk deletion + re-embed happens in
        # the batch pass at end of index_all. This keeps per-paper work
        # cheap.
        self.store.execute(
            "UPDATE papers SET embedded = 0 WHERE paper_key = ?",
            (paper_key,)
        )
        self._pending_embed.append(paper_key)

        self.store.commit()

        if is_new:
            report.new += 1
        else:
            report.updated += 1

    def _replace_paper_attachments(self, paper_key: str, fm: dict) -> None:
        self.store.execute(
            "DELETE FROM paper_attachments WHERE paper_key = ?", (paper_key,)
        )
        att_keys = fm.get("zotero_attachment_keys") or []
        main_key = fm.get("zotero_main_attachment_key")
        rows = []
        for i, ak in enumerate(att_keys):
            if not isinstance(ak, str):
                continue
            rows.append((ak, paper_key, 1 if ak == main_key else 0, i))
        if rows:
            self.store.executemany("""
                INSERT OR REPLACE INTO paper_attachments
                    (attachment_key, paper_key, is_main, position)
                VALUES (?, ?, ?, ?)
            """, rows)

    def _replace_paper_tags(self, paper_key: str, fm: dict) -> None:
        self.store.execute(
            "DELETE FROM paper_tags WHERE paper_key = ?", (paper_key,)
        )
        rows = []
        for t in fm.get("zotero_tags") or []:
            if isinstance(t, str) and t:
                rows.append((paper_key, t, "zotero"))
        for t in fm.get("kb_tags") or []:
            if isinstance(t, str) and t:
                rows.append((paper_key, t, "kb"))
        if rows:
            self.store.executemany("""
                INSERT OR IGNORE INTO paper_tags (paper_key, tag, source)
                VALUES (?, ?, ?)
            """, rows)

    def _replace_paper_collections(self, paper_key: str, fm: dict) -> None:
        self.store.execute(
            "DELETE FROM paper_collections WHERE paper_key = ?", (paper_key,)
        )
        rows = []
        for c in fm.get("zotero_collections") or []:
            if isinstance(c, str) and c:
                rows.append((paper_key, c))
        if rows:
            self.store.executemany("""
                INSERT OR IGNORE INTO paper_collections
                    (paper_key, collection_name)
                VALUES (?, ?)
            """, rows)

    def _replace_paper_fts(
        self, paper_key: str, fm: dict, abstract: str, fulltext: str
    ) -> None:
        # FTS5 virtual tables don't support PK-based upsert. We delete
        # by paper_key then re-insert.
        self.store.execute(
            "DELETE FROM paper_fts WHERE paper_key = ?", (paper_key,)
        )
        authors_flat = ", ".join(
            a for a in (fm.get("authors") or []) if isinstance(a, str)
        )
        self.store.execute("""
            INSERT INTO paper_fts
                (paper_key, title, authors, abstract, fulltext)
            VALUES (?, ?, ?, ?, ?)
        """, (
            paper_key,
            fm.get("title") or "",
            authors_flat,
            abstract,
            fulltext,
        ))

    # --- notes ---

    def _index_notes(self, report: IndexReport) -> None:
        # v26: standalone Zotero notes live under topics/standalone-note/
        # (was zotero-notes/ in v25; legacy location is flagged as
        # deprecated by index-status, not scanned here).
        d = self.kb_root / TOPICS_STANDALONE_DIR
        if not d.exists():
            return
        for md in sorted(d.glob("*.md")):
            if md.name.startswith("."):
                continue
            if not self._should_process(md):
                continue
            try:
                self._index_note(md, report)
            except Exception as e:
                log.exception("failed indexing note %s", md.name)
                report.errors.append((str(md.relative_to(self.kb_root)), str(e)))

    def _index_note(self, md: Path, report: IndexReport) -> None:
        key = md.stem
        md_rel = md.relative_to(self.kb_root).as_posix()
        md_mtime = md.stat().st_mtime

        row = self.store.execute(
            "SELECT md_mtime FROM notes WHERE zotero_key = ?", (key,)
        ).fetchone()
        if row and abs(row["md_mtime"] - md_mtime) < 1e-6:
            report.unchanged += 1
            return
        is_new = row is None

        post = frontmatter.load(str(md))
        # v27: accept both kind values. The canonical value since
        # v27 is "note"; earlier (v21..v26) md files were written
        # with "zotero_standalone_note" and may still exist on disk
        # without re-import. Both map to the same schema row; no
        # migration required.
        kind = post.metadata.get("kind")
        if kind not in ("note", "zotero_standalone_note"):
            return

        self.store.execute("""
            INSERT OR REPLACE INTO notes
                (zotero_key, title, md_path, md_mtime, last_indexed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            key,
            post.metadata.get("title") or "",
            md_rel, md_mtime, _now_iso(),
        ))
        # Phase 2c: stage links. Notes' body is usually about the
        # content of an external paper (bibtex @refs to external
        # works), NOT cross-references between KB items. Disable
        # @cite extraction to avoid false-positive edges.
        self._stage_refs(
            "note", key, post.metadata, post.content,
            include_cite=False,
        )
        self.store.commit()
        if is_new:
            report.new += 1
        else:
            report.updated += 1

    # --- topics ---

    def _index_topics(self, report: IndexReport) -> None:
        # v26: AI-generated topic syntheses live under
        # topics/agent-created/ (was topics/<slug>.md top-level in v25).
        # Top-level topics/*.md is flagged as deprecated by
        # index-status; not scanned here.
        d = self.kb_root / TOPICS_AGENT_DIR
        if not d.exists():
            return
        # topics/agent-created/ may itself have subdirs
        # (e.g. stability/overview.md); recurse.
        for md in sorted(d.rglob("*.md")):
            if md.name.startswith("."):
                continue
            if not self._should_process(md):
                continue
            try:
                self._index_topic(md, report)
            except Exception as e:
                log.exception("failed indexing topic %s", md.name)
                report.errors.append((str(md.relative_to(self.kb_root)), str(e)))

    def _index_topic(self, md: Path, report: IndexReport) -> None:
        md_rel = md.relative_to(self.kb_root).as_posix()
        md_mtime = md.stat().st_mtime
        # For topics, slug is path-relative to the agent-created
        # bucket (e.g. "stability/overview"); this preserves the v25
        # slug semantics so backlinks from older md content still
        # resolve after reorganisation.
        slug = md.relative_to(self.kb_root / TOPICS_AGENT_DIR).with_suffix("").as_posix()

        row = self.store.execute(
            "SELECT md_mtime FROM topics WHERE slug = ?", (slug,)
        ).fetchone()
        if row and abs(row["md_mtime"] - md_mtime) < 1e-6:
            report.unchanged += 1
            return
        is_new = row is None

        post = frontmatter.load(str(md))
        self.store.execute("""
            INSERT OR REPLACE INTO topics
                (slug, title, md_path, md_mtime, last_indexed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            slug,
            post.metadata.get("title") or "",
            md_rel, md_mtime, _now_iso(),
        ))
        # Phase 2c: topics heavily link out via kb_refs + wikilinks.
        self._stage_refs("topic", slug, post.metadata, post.content)
        self.store.commit()
        if is_new:
            report.new += 1
        else:
            report.updated += 1

    # --- thoughts ---

    def _index_thoughts(self, report: IndexReport) -> None:
        d = self.kb_root / THOUGHTS_DIR
        if not d.exists():
            return
        for md in sorted(d.glob("*.md")):
            if md.name.startswith("."):
                continue
            if not self._should_process(md):
                continue
            try:
                self._index_thought(md, report)
            except Exception as e:
                log.exception("failed indexing thought %s", md.name)
                report.errors.append((str(md.relative_to(self.kb_root)), str(e)))

    def _index_thought(self, md: Path, report: IndexReport) -> None:
        slug = md.stem
        md_rel = md.relative_to(self.kb_root).as_posix()
        md_mtime = md.stat().st_mtime

        row = self.store.execute(
            "SELECT md_mtime FROM thoughts WHERE slug = ?", (slug,)
        ).fetchone()
        if row and abs(row["md_mtime"] - md_mtime) < 1e-6:
            report.unchanged += 1
            return
        is_new = row is None

        post = frontmatter.load(str(md))
        self.store.execute("""
            INSERT OR REPLACE INTO thoughts
                (slug, title, md_path, md_mtime, created_at, last_indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            slug,
            post.metadata.get("title") or "",
            md_rel, md_mtime,
            post.metadata.get("created_at") or "",
            _now_iso(),
        ))
        # Phase 2c: thoughts are the densest link source — AI notes,
        # connections between papers, references to topics.
        self._stage_refs("thought", slug, post.metadata, post.content)
        self.store.commit()
        if is_new:
            report.new += 1
        else:
            report.updated += 1

    # -----------------------------------------------------------------
    # Orphan cleanup
    # -----------------------------------------------------------------

    def _remove_orphans(self, report: IndexReport) -> None:
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
            rows = self.store.execute(
                f"SELECT {pk_col} AS pk, md_path FROM {table}"
            ).fetchall()
            removed_keys = []
            for r in rows:
                if not (self.kb_root / r["md_path"]).exists():
                    removed_keys.append(r["pk"])
            if removed_keys:
                # ON DELETE CASCADE takes care of dependent rows for papers.
                placeholders = ",".join("?" * len(removed_keys))
                self.store.execute(
                    f"DELETE FROM {table} WHERE {pk_col} IN ({placeholders})",
                    tuple(removed_keys),
                )
                # FTS doesn't have FK; clean it too.
                if table == "papers":
                    self.store.execute(
                        f"DELETE FROM paper_fts WHERE paper_key IN ({placeholders})",
                        tuple(removed_keys),
                    )
                    # Phase 2b: clean chunk meta + vec. chunk_meta has
                    # FK ON DELETE CASCADE to papers, but we don't run
                    # with PRAGMA foreign_keys=ON everywhere, so be
                    # explicit. Vec0 doesn't support FK at all.
                    chunk_ids = [
                        r["chunk_id"] for r in self.store.execute(
                            f"SELECT chunk_id FROM paper_chunk_meta "
                            f"WHERE paper_key IN ({placeholders})",
                            tuple(removed_keys),
                        ).fetchall()
                    ]
                    self.store.execute(
                        f"DELETE FROM paper_chunk_meta "
                        f"WHERE paper_key IN ({placeholders})",
                        tuple(removed_keys),
                    )
                    if chunk_ids and self.store.vec_available:
                        cid_placeholders = ",".join("?" * len(chunk_ids))
                        self.store.execute(
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
                self.store.execute(
                    f"DELETE FROM links "
                    f"WHERE src_type = ? AND src_key IN ({placeholders})",
                    (lt, *removed_keys),
                )
                # Incoming edges become dangling rather than disappear:
                # a deleted paper might come back, and edges from other
                # nodes pointing at it are still meaningful metadata.
                self.store.execute(
                    f"UPDATE links SET dst_type = 'dangling' "
                    f"WHERE dst_type = ? AND dst_key IN ({placeholders})",
                    (lt, *removed_keys),
                )
                self.store.commit()
                report.removed += len(removed_keys)

    # -----------------------------------------------------------------
    # Embedding pass (Phase 2b)
    # -----------------------------------------------------------------

    def _run_embedding_pass(self, report: IndexReport) -> None:
        """Batch-embed all papers queued during this index run.

        Deduplicates (same paper indexed twice in one run would be a
        bug, but be defensive). Skips silently if no provider or vec
        unavailable. Per-paper failures are tolerated: the paper keeps
        embedded=0 so next run can retry.
        """
        if not self._pending_embed:
            return

        # Dedupe, preserve order.
        seen: set[str] = set()
        pending = [
            k for k in self._pending_embed
            if k not in seen and not seen.add(k)
        ]
        self._pending_embed = []

        if self._embedder is None:
            report.embed_skipped = len(pending)
            log.info(
                "Skipping embedding pass for %d paper(s): "
                "no embedding provider configured.", len(pending),
            )
            return

        if not self.store.vec_available:
            report.embed_skipped = len(pending)
            log.warning(
                "Skipping embedding pass for %d paper(s): "
                "sqlite-vec extension not loaded.", len(pending),
            )
            return

        log.info(
            "Embedding %d paper(s) with model %s ...",
            len(pending), self._embedder.model_name,
        )

        # Gather chunks from all papers first, then batch.
        # Struct: list of (paper_key, chunk_meta_tuple, text)
        # chunk_meta_tuple = (kind, section_num, section_title)
        all_chunks: list[tuple[str, tuple, str]] = []
        for pk in pending:
            try:
                chunks = self._chunk_paper(pk)
            except Exception as e:
                log.warning("Could not chunk paper %s: %s", pk, e)
                report.embed_failed += 1
                continue
            for meta, text in chunks:
                all_chunks.append((pk, meta, text))

        if not all_chunks:
            log.info("No embeddable content across %d paper(s).", len(pending))
            return

        # Flush old chunk rows for all pending papers in one shot.
        placeholders = ",".join("?" * len(pending))
        old_ids = [
            r["chunk_id"] for r in self.store.execute(
                f"SELECT chunk_id FROM paper_chunk_meta "
                f"WHERE paper_key IN ({placeholders})",
                tuple(pending),
            ).fetchall()
        ]
        self.store.execute(
            f"DELETE FROM paper_chunk_meta WHERE paper_key IN ({placeholders})",
            tuple(pending),
        )
        if old_ids:
            cid_ph = ",".join("?" * len(old_ids))
            self.store.execute(
                f"DELETE FROM paper_chunks_vec WHERE chunk_id IN ({cid_ph})",
                tuple(old_ids),
            )

        # Call API in batches.
        success_papers: set[str] = set()
        for batch_start in range(0, len(all_chunks), self._batch_size):
            batch = all_chunks[batch_start:batch_start + self._batch_size]
            texts = [t for (_pk, _meta, t) in batch]
            try:
                result = self._embedder.embed(texts)
            except Exception as e:
                # One batch failed. Mark all papers in this batch as
                # failed so they're retried next run. Continue with
                # other batches rather than aborting the whole pass.
                log.warning(
                    "Embedding batch failed (%d texts): %s",
                    len(texts), e,
                )
                for pk, _meta, _t in batch:
                    report.embed_failed += 1 if pk not in success_papers else 0
                continue

            report.embed_api_calls += 1
            report.embed_tokens += result.prompt_tokens

            # Insert chunk_meta rows (autoincrement gives chunk_id),
            # then the corresponding vec rows.
            for (pk, meta, text), vec in zip(batch, result.vectors):
                kind, section_num, section_title = meta
                cur = self.store.execute(
                    "INSERT INTO paper_chunk_meta "
                    "(paper_key, kind, section_num, section_title, text) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pk, kind, section_num, section_title, text),
                )
                chunk_id = cur.lastrowid
                self.store.execute(
                    "INSERT INTO paper_chunks_vec (chunk_id, embedding) "
                    "VALUES (?, ?)",
                    (chunk_id, _vec_blob(vec)),
                )
                success_papers.add(pk)
                report.embedded_chunks += 1

        # Mark successfully-embedded papers in the papers table.
        # v26: the PK is paper_key (md stem), matching what we pushed
        # onto _pending_embed and what survived the embedding call.
        if success_papers:
            placeholders = ",".join("?" * len(success_papers))
            self.store.execute(
                f"UPDATE papers SET embedded = 1, "
                f"embedding_model = ?, embedded_at = ? "
                f"WHERE paper_key IN ({placeholders})",
                (self._embedder.model_name, _now_iso(), *success_papers),
            )
        report.embedded_papers = len(success_papers)
        self.store.commit()
        log.info(
            "Embedded %d papers → %d chunks in %d API call(s), %d tokens total.",
            report.embedded_papers, report.embedded_chunks,
            report.embed_api_calls, report.embed_tokens,
        )

    def _chunk_paper(self, paper_key: str) -> list[tuple[tuple, str]]:
        """Split a paper into (meta, text) tuples for embedding.

        Strategy:
        1. "header" chunk: title + authors + abstract as one text.
           Always emitted (even if abstract empty) so papers with no
           fulltext yet are still findable by semantic search.
        2. "section" chunks: one per `## N. ...` heading in the
           fulltext region. Preserves the 7-section structure of
           kb-importer summaries; each section becomes its own vector.
           If no sections match (e.g. fulltext is empty or free-form),
           the whole fulltext body becomes a single section-0 chunk.

        Returns list of ((kind, section_num, section_title), text).
        Each text is clamped to ~6000 chars to stay comfortably under
        OpenAI's 8192-token limit (chars/token ~= 2 for EN, ~=1 for
        CJK so this is a safe lower bound).
        """
        row = self.store.execute(
            "SELECT title, authors, abstract, md_path FROM papers "
            "WHERE paper_key = ?",
            (paper_key,)
        ).fetchone()
        if row is None:
            return []

        md_full = (self.kb_root / row["md_path"]).read_text(encoding="utf-8")
        # Strip frontmatter — we don't want to embed the yaml.
        content = _strip_frontmatter(md_full)
        fulltext = _extract_fulltext_body(content)

        out: list[tuple[tuple, str]] = []

        # Header chunk: title + authors + abstract.
        authors_flat = _authors_flat(row["authors"])
        header_parts = [row["title"] or ""]
        if authors_flat:
            header_parts.append(f"Authors: {authors_flat}")
        if row["abstract"]:
            header_parts.append(row["abstract"])
        header_text = "\n\n".join(p for p in header_parts if p).strip()
        if header_text:
            out.append((
                ("header", None, None),
                _clamp(header_text),
            ))

        if fulltext:
            sections = _split_fulltext_sections(fulltext)
            if sections:
                for section_num, section_title, section_text in sections:
                    # Prepend section title so the vector "knows" what
                    # section it is — helps queries like "find me
                    # methods sections discussing X".
                    full = f"{section_title}\n\n{section_text}".strip()
                    out.append((
                        ("section", section_num, section_title),
                        _clamp(full),
                    ))
            else:
                # Fulltext present but not in 7-section format.
                # Emit as single chunk (section_num=0 means "whole").
                out.append((
                    ("section", 0, "Fulltext"),
                    _clamp(fulltext),
                ))

        return out

    # -----------------------------------------------------------------
    # Link graph (Phase 2c)
    # -----------------------------------------------------------------

    def _stage_refs(
        self,
        src_type: str,
        src_key: str,
        fm: dict,
        body: str,
        *,
        include_cite: bool = True,
    ) -> None:
        """Extract outbound refs from one md and queue them for the
        post-pass resolver.

        We don't insert into links here because resolution needs ALL
        node tables populated (a thought might reference a paper we
        haven't reached yet in this run). Deferring also lets us
        batch-insert at the end.

        Called from each _index_* method after the core row is written.
        """
        refs = extract_refs(fm, body, include_cite=include_cite)
        self._touched_srcs.add((src_type, src_key))
        for ref in refs:
            self._staged_links.append((src_type, src_key, ref))

    def _resolve_staged_links(self, report: IndexReport) -> None:
        """Second pass: turn ExtractedRef candidates into typed edges.

        Algorithm:
          1. Clear existing links for every src that had its md
             re-indexed this run (_touched_srcs). This is narrower
             than wiping the whole table — unchanged mds keep their
             edges intact.
          2. Build lookup maps for each node type (one SELECT each).
          3. For each (src, ref) in _staged_links, resolve dst_type:
             - honour ref.hint_type if present and lookup succeeds;
             - else try paper → topic → thought → note;
             - else mark as 'dangling' with ref.key verbatim.
          4. Batch-insert into links, deduping on full PK.

        After this pass, `kb-mcp index` again will automatically promote
        dangling edges to real ones if the missing target got added
        (cached lookup maps are rebuilt each run).
        """
        if not self._touched_srcs and not self._staged_links:
            return

        # 1. Purge old edges for touched srcs.
        if self._touched_srcs:
            # Chunked delete to avoid SQL parameter limits (~999).
            srcs = list(self._touched_srcs)
            for start in range(0, len(srcs), 200):
                batch = srcs[start:start + 200]
                # Build "(?, ?) OR (?, ?) OR ..." safely via tuple values.
                placeholders = " OR ".join(
                    "(src_type = ? AND src_key = ?)" for _ in batch
                )
                params = [v for pair in batch for v in pair]
                # Preserve citation edges (written separately by
                # `kb-citations link`, not re-staged by the indexer).
                # Without this filter, every re-index of a paper
                # silently drops its citation out-edges until the
                # user runs `kb-citations link` again.
                self.store.execute(
                    f"DELETE FROM links WHERE ({placeholders}) "
                    f"AND origin != 'citation'",
                    tuple(params),
                )

        if not self._staged_links:
            self.store.commit()
            return

        # 2. Lookup maps. For each node type we just need "does X
        # exist?". citation_key gets a dedicated map for @cite refs.
        # v26: papers keyed by paper_key (md stem) because that's
        # what kb_refs addresses resolve to (kb_refs values look
        # like "papers/BOOKKEY-ch03", whose tail is the paper_key).
        paper_keys = {r["paper_key"] for r in self.store.execute(
            "SELECT paper_key FROM papers"
        ).fetchall()}
        note_keys = {r["zotero_key"] for r in self.store.execute(
            "SELECT zotero_key FROM notes"
        ).fetchall()}
        topic_slugs = {r["slug"] for r in self.store.execute(
            "SELECT slug FROM topics"
        ).fetchall()}
        thought_slugs = {r["slug"] for r in self.store.execute(
            "SELECT slug FROM thoughts"
        ).fetchall()}
        # v26: @cite resolves to the WHOLE-work paper row, not a
        # chapter row. Rows where paper_key != zotero_key are
        # chapter siblings; they inherit citation_key from the
        # parent through frontmatter copy but we only want the
        # parent to be a valid @cite target. Filtering by
        # paper_key = zotero_key gives us exactly the whole-work
        # rows (single-md papers and the top row of multi-md works).
        citation_to_paper: dict[str, str] = {
            r["citation_key"]: r["paper_key"]
            for r in self.store.execute(
                "SELECT citation_key, paper_key FROM papers "
                "WHERE citation_key IS NOT NULL AND citation_key != '' "
                "AND paper_key = zotero_key"
            ).fetchall()
        }

        # 3 + 4. Resolve and batch insert.
        rows: list[tuple] = []
        dangling_count = 0
        seen: set[tuple] = set()
        for src_type, src_key, ref in self._staged_links:
            dst_type, dst_key = _resolve_one(
                ref,
                paper_keys, note_keys, topic_slugs, thought_slugs,
                citation_to_paper,
            )
            # Skip self-loops.
            if (src_type, src_key) == (dst_type, dst_key):
                continue
            pk = (src_type, src_key, dst_type, dst_key, ref.origin)
            if pk in seen:
                continue
            seen.add(pk)
            rows.append(pk)
            if dst_type == "dangling":
                dangling_count += 1

        if rows:
            self.store.executemany(
                "INSERT OR IGNORE INTO links "
                "(src_type, src_key, dst_type, dst_key, origin) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        self.store.commit()

        report.links_written = len(rows)
        report.links_dangling = dangling_count
        log.info(
            "Resolved %d link edge(s), %d dangling.",
            len(rows), dangling_count,
        )

        # Clear state so another index_all() call starts fresh.
        self._staged_links = []
        self._touched_srcs = set()


def _resolve_one(
    ref: ExtractedRef,
    paper_keys: set[str],
    note_keys: set[str],
    topic_slugs: set[str],
    thought_slugs: set[str],
    citation_to_paper: dict[str, str],
) -> tuple[str, str]:
    """Classify a single ExtractedRef into (dst_type, dst_key).

    Never raises. Unknown targets become ('dangling', ref.key) so
    they stay visible in the graph and can be re-resolved later.
    """
    # @cite → always a paper lookup via citation_key.
    if ref.origin == "cite":
        paper_key = citation_to_paper.get(ref.key)
        if paper_key is not None:
            return ("paper", paper_key)
        return ("dangling", ref.key)

    # hint_type set (from subdir in frontmatter/wikilink/mdlink).
    if ref.hint_type is not None:
        if _exists(ref.hint_type, ref.key,
                   paper_keys, note_keys, topic_slugs, thought_slugs):
            return (ref.hint_type, ref.key)
        # Hinted but not found → dangling (don't silently fall through
        # to a different type, that would be confusing).
        return ("dangling", ref.key)

    # No hint: try each node type in order of likelihood.
    for candidate in ("paper", "topic", "thought", "note"):
        if _exists(candidate, ref.key,
                   paper_keys, note_keys, topic_slugs, thought_slugs):
            return (candidate, ref.key)
    return ("dangling", ref.key)


def _exists(
    node_type: str, key: str,
    paper_keys: set[str], note_keys: set[str],
    topic_slugs: set[str], thought_slugs: set[str],
) -> bool:
    if node_type == "paper":
        return key in paper_keys
    if node_type == "note":
        return key in note_keys
    if node_type == "topic":
        return key in topic_slugs
    if node_type == "thought":
        return key in thought_slugs
    return False


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

_FULLTEXT_PATTERN = re.compile(
    re.escape(FULLTEXT_START) + r"(.*?)" + re.escape(FULLTEXT_END),
    flags=re.DOTALL,
)


def _extract_fulltext_body(content: str) -> str:
    m = _FULLTEXT_PATTERN.search(content)
    if not m:
        return ""
    body = m.group(1).strip()
    # Treat the placeholder comment as empty.
    if "Empty when fulltext_processed=false" in body:
        return ""
    return body


def _extract_abstract(content: str) -> str:
    """Pull abstract text between '## Abstract' heading and next '##'.

    Loose heuristic: if there's no '## Abstract' heading, returns "".
    """
    m = re.search(
        r"##\s+Abstract\s*\n(.*?)(?=\n##\s+|\Z)",
        content,
        flags=re.DOTALL,
    )
    if not m:
        return ""
    # Strip the zotero-field marker comment kb-importer leaves.
    text = re.sub(r"<!--\s*zotero-field:.*?-->", "", m.group(1)).strip()
    return text


def _safe_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------
# Phase 2b helpers: chunking + vector serialization
# ---------------------------------------------------------------------

# Match headings of the form "## 1. <title>" through "## 7. <title>"
# in the Chinese-language section summary format (see
# kb_importer/templates/ai_summary_prompt.md). The number lets us
# attach section_num; content extends to the next "^## N." line or
# end of text. DOTALL so .*? can span newlines.
_SECTION_RE = re.compile(
    r"^##\s+(\d+)\.\s*(.+?)\n(.*?)(?=^##\s+\d+\.\s|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)


def _split_fulltext_sections(text: str) -> list[tuple[int, str, str]]:
    """Return [(num, title, body), ...] from "## N. ..." headings.

    Empty list if no matches (caller should treat as single chunk).
    Title is trimmed; body has whitespace stripped on both ends.
    """
    out: list[tuple[int, str, str]] = []
    for m in _SECTION_RE.finditer(text):
        num = int(m.group(1))
        title = m.group(2).strip()
        body = m.group(3).strip()
        if body:
            out.append((num, title, body))
    return out


def _strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block if present."""
    if not content.startswith("---\n"):
        return content
    # Find the closing ---
    end = content.find("\n---\n", 4)
    if end < 0:
        return content
    return content[end + 5:]


def _clamp(text: str, max_chars: int = 6000) -> str:
    """Hard cap on chunk size to stay under embedding model token limit."""
    if len(text) <= max_chars:
        return text
    # Cut at a paragraph boundary near the limit when possible.
    cut = text.rfind("\n\n", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    return text[:cut] + "\n\n…[truncated]"


def _authors_flat(authors_json: str | None) -> str:
    """Turn the JSON array stored in papers.authors into a flat string."""
    if not authors_json:
        return ""
    try:
        import json
        parsed = json.loads(authors_json)
        if isinstance(parsed, list):
            return ", ".join(str(a) for a in parsed if a)
    except Exception:
        pass
    return ""


def _vec_blob(vec: list[float]) -> bytes:
    """Serialize a float vector for sqlite-vec.

    vec0 accepts either a list (via sqlite-vec's type coercion) or a
    packed bytes blob. We use struct-packed float32 for portability
    and size (4 bytes × dim vs. Python list overhead). This matches
    the format documented for sqlite-vec.
    """
    import struct
    return struct.pack(f"{len(vec)}f", *vec)
