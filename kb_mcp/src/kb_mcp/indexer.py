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
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from .paths import (
    TOPICS_STANDALONE_DIR, TOPICS_AGENT_DIR, THOUGHTS_DIR, is_book_chapter_filename,
)
from .store import Store

# v0.28.0: per-concern submodules. Indexer keeps the walk + per-md
# dispatch; these handle the three heavy passes. Thin delegate
# methods (_run_embedding_pass, _remove_orphans, _resolve_staged_links)
# forward self/indexer + report.
from . import embedding_pass as _embedding_pass
from . import stale_cleanup as _stale_cleanup
from . import link_resolve as _link_resolve

# v0.28.0: helper functions moved to `_indexer_helpers` so that the
# per-pass submodules can share them without importing `indexer`
# (would be circular). We re-export the private names here so any
# external caller `from kb_mcp.indexer import _extract_fulltext_body`
# still works.
from ._indexer_helpers import (
    _extract_fulltext_body, _extract_abstract,
    _safe_int, _now_iso,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Section markers used by kb-importer's paper md template.
# v0.28.0: imported from the canonical kb_core source rather than
# re-declared. Re-exported here for callers that `from
# kb_mcp.indexer import FULLTEXT_START`.
# ---------------------------------------------------------------------


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
    # v0.28.2: dangling-promotion pass (see link_resolve._promote_dangling_edges).
    links_promoted: int = 0         # dangling edges that resolved on this run

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
                # v0.27.10: if the failure happened on a paper that
                # we had a stale row for, clean the row so search /
                # backlinks don't keep returning a phantom node
                # every run. (Idempotent no-op when there was no
                # row.)
                try:
                    self._delete_stale_node_row("papers", md.stem)
                except Exception:
                    log.exception(
                        "stale-row cleanup failed for paper %s",
                        md.name,
                    )

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
            # v0.27.10: if we had a stale row, clean it so the
            # phantom paper stops showing up in search / backlinks
            # / graph. Pre-0.27.10 the row stayed until the md was
            # deleted from disk, even though the indexer had
            # already decided it was no longer a paper.
            if row is not None:
                self._delete_stale_node_row("papers", paper_key)
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
                try:
                    self._delete_stale_node_row("notes", md.stem)
                except Exception:
                    log.exception(
                        "stale-row cleanup failed for note %s",
                        md.name,
                    )

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
            # v0.27.10: kind changed (or was corrupted) — clean
            # any stale row so it stops being returned by queries.
            if row is not None:
                self._delete_stale_node_row("notes", key)
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
                # Topic slug derivation uses a relative path; best
                # effort here — if we can't compute it, skip cleanup.
                try:
                    slug = md.relative_to(
                        self.kb_root / TOPICS_AGENT_DIR
                    ).with_suffix("").as_posix()
                    self._delete_stale_node_row("topics", slug)
                except Exception:
                    log.exception(
                        "stale-row cleanup failed for topic %s",
                        md.name,
                    )

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
                try:
                    self._delete_stale_node_row("thoughts", md.stem)
                except Exception:
                    log.exception(
                        "stale-row cleanup failed for thought %s",
                        md.name,
                    )

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
    # Stale-row cleanup on in-place frontmatter changes
    # -----------------------------------------------------------------

    # Map table → (pk_col, src_type for links)
    _NODE_TABLE_META = {
        "papers":   ("paper_key",  "paper"),
        "notes":    ("zotero_key", "note"),
        "topics":   ("slug",       "topic"),
        "thoughts": ("slug",       "thought"),
    }

    def _delete_stale_node_row(self, table: str, key: str) -> bool:
        """Thin delegate → stale_cleanup.delete_stale_node_row."""
        return _stale_cleanup.delete_stale_node_row(self, table, key)

    # -----------------------------------------------------------------
    # Orphan cleanup
    # -----------------------------------------------------------------

    def _remove_orphans(self, report: IndexReport) -> None:
        """Thin delegate → stale_cleanup.remove_orphans."""
        _stale_cleanup.remove_orphans(self, report)

    # -----------------------------------------------------------------
    # Embedding pass (Phase 2b)
    # -----------------------------------------------------------------

    def _run_embedding_pass(self, report: IndexReport) -> None:
        """Thin delegate → embedding_pass.run_embedding_pass."""
        _embedding_pass.run_embedding_pass(self, report)

    def _chunk_paper(self, paper_key: str) -> list[tuple[tuple, str]]:
        """Thin delegate → embedding_pass.chunk_paper."""
        return _embedding_pass.chunk_paper(self, paper_key)
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
        """Thin delegate → link_resolve.stage_refs."""
        _link_resolve.stage_refs(
            self, src_type, src_key, fm, body, include_cite=include_cite,
        )

    def _resolve_staged_links(self, report: IndexReport) -> None:
        """Thin delegate → link_resolve.resolve_staged_links."""
        _link_resolve.resolve_staged_links(self, report)

