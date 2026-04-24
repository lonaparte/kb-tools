"""SQLite store for kb-mcp's projection layer.

This module owns:
- Creating and migrating the DB file.
- Providing a typed connection to the rest of the codebase.
- Schema version tracking (so we know when the on-disk schema diverges
  from what this code expects and a rebuild is needed).

Phase 2a scope: Core tables + FTS5. No vector support yet (Phase 2b
adds sqlite-vec).

The DB file lives at `<kb_root>/.kb-mcp/index.sqlite` by default. It's
a derivative artefact — the md files are the source of truth — so
deleting it just triggers a re-index on next `kb-mcp index` run.
"""
from __future__ import annotations

import logging
import sqlite3
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


# Schema version this code expects. Bumped manually whenever schema.sql
# gets an incompatible change. If the DB has a lower version, we drop
# and rebuild (safe because the DB is derived from md).
#
# Version history (each line documents what the bump added; migration
# is always drop-and-rebuild from md, so "additive" vs "breaking"
# distinction doesn't matter here — but recording the change set
# makes it possible to tell whether an older codebase can read a
# newer DB).
#
#   v1 = Phase 2a: initial `papers` + `paper_fts` (FTS5 only).
#   v2 = Phase 2b: added `paper_chunk_meta` (AUTOINCREMENT chunk_id),
#                  `paper_chunks_vec` (sqlite-vec virtual),
#                  `papers.embedded` / `papers.embedding_model` /
#                  `papers.embedded_at` columns.
#   v3 = Phase 2c: added `links` table (+ idx_links_src, idx_links_dst)
#                  for the wikilink / mdlink / cite / citation graph.
#   v4 = Phase 3 relational slice: split `papers.authors` /
#                  `papers.tags` / `papers.collections` out into
#                  `paper_attachments`, `paper_tags`, `paper_collections`
#                  side tables so queries like "papers tagged X"
#                  don't have to JSON-parse a blob.
#   v5 = Phase 4: added citation_count + citation_count_source +
#                  citation_count_updated_at columns to `papers`
#                  (+ idx_papers_citation_count) for the kb-citations
#                  refresh-counts path. `links.origin` CHECK also
#                  gained 'citation' as a valid value.
#
#   v6 = v26 data model refactor: papers primary key moved from
#                  `zotero_key` to `paper_key` (the md stem), and
#                  `zotero_key` became a non-unique index column.
#                  This enables one Zotero item to yield multiple
#                  md rows (e.g. a book at papers/BOOKKEY.md plus
#                  its chapters at papers/BOOKKEY-chNN.md, all
#                  sharing zotero_key = "BOOKKEY"). No other
#                  schema changes — side tables still reference
#                  papers via `paper_key`, same column name, new
#                  referent.
#   v7 = FK-target fix for v6 regression. When v6 made
#                  `papers.zotero_key` non-unique, the four side
#                  tables (paper_attachments / paper_tags /
#                  paper_collections / paper_chunk_meta) still
#                  carried `REFERENCES papers(zotero_key)` from v5.
#                  SQLite's FK checker requires the target to be
#                  PK or UNIQUE; non-unique zotero_key meant every
#                  INSERT into those side tables tripped
#                  "foreign key mismatch". v7 repoints all four
#                  FKs to `papers(paper_key)` (the new PK). No
#                  column adds/drops; schema.sql diff is 4 × 1
#                  line change, all in FK clauses. Shipped with
#                  0.27.1; first correctly-documented in 0.28.2.
#
# When bumping this constant, add a `v<N+1> = ...` line above and
# describe the change set in one line. A missing entry is a lint
# failure waiting to happen — every bump must be documented.
EXPECTED_SCHEMA_VERSION = 7


class Store:
    """Wraps a SQLite connection with helpers.

    Not thread-safe; each process should hold one Store. The kb-mcp
    server is single-threaded (stdio MCP) so this is fine.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        journal_mode: str | None = None,
        vec_dim: int = 1536,
    ):
        """Open (or create) the projection DB.

        Args:
            db_path: path to the SQLite file.
            journal_mode: 'delete' (default, portable single-file DB)
                or 'wal' (better concurrent reads during writes).
                Pass None to use the default. Case-insensitive.
            vec_dim: dimensionality of the paper_chunks_vec table.
                MUST match the embedding provider's output. Default
                1536 (OpenAI text-embedding-3-small, Gemini MRL
                truncation default). Set to 3072 for
                text-embedding-3-large @ full dim, etc. Only used
                when creating the vec table; existing tables keep
                their original dimension.
        """
        self.db_path = db_path
        self.vec_dim = int(vec_dim)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Journal mode. Default DELETE keeps the DB as a single
        # self-contained file — `.kb-mcp/` stays rsync-/Syncthing-
        # portable with no WAL/SHM sidecars to worry about.
        # WAL is preferred for true multi-process concurrent workloads
        # (e.g. long-running MCP server alongside frequent re-indexes).
        # Override via Store(..., journal_mode="wal") or the
        # `store.journal_mode` key in kb-mcp.yaml.
        mode = (journal_mode or "DELETE").upper()
        if mode not in ("DELETE", "WAL"):
            log.warning(
                "Unknown journal_mode %r; falling back to DELETE. "
                "Supported: delete | wal.", journal_mode,
            )
            mode = "DELETE"
        self.conn.execute(f"PRAGMA journal_mode = {mode}")
        self.journal_mode = mode

        # Phase 2b: load sqlite-vec extension so CREATE VIRTUAL TABLE
        # ... USING vec0 and the vec0 query functions work.
        # The extension ships as a Python package that exposes a
        # `load` function taking the sqlite3 connection.
        # Failure here is non-fatal — we log and proceed; vec-backed
        # tools will fail loudly later if they're actually used.
        self.vec_available = _try_load_sqlite_vec(self.conn)

    def close(self) -> None:
        self.conn.close()

    # -----------------------------------------------------------------
    # Schema management
    # -----------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Apply schema.sql; rebuild from scratch if version mismatch.

        Idempotent on matching version. Safe to call at server start or
        indexer start — both paths should invoke this.
        """
        current = self._current_version()
        if current is None:
            log.info("Fresh DB at %s; applying schema.", self.db_path)
            self._apply_schema()
            self._stamp_version(EXPECTED_SCHEMA_VERSION)
            return

        if current == EXPECTED_SCHEMA_VERSION:
            return  # already up-to-date

        if current < EXPECTED_SCHEMA_VERSION:
            log.warning(
                "DB schema v%d older than code v%d; rebuilding.",
                current, EXPECTED_SCHEMA_VERSION,
            )
            self._drop_all()
            self._apply_schema()
            self._stamp_version(EXPECTED_SCHEMA_VERSION)
            return

        # current > expected: DB was written by a newer kb-mcp. Refuse
        # rather than corrupt — user should upgrade kb-mcp.
        raise RuntimeError(
            f"DB schema version {current} is newer than this code "
            f"supports (v{EXPECTED_SCHEMA_VERSION}). Upgrade kb-mcp or "
            f"delete {self.db_path} to rebuild from scratch."
        )

    def _current_version(self) -> int | None:
        try:
            row = self.conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
        except sqlite3.OperationalError:
            # Table doesn't exist yet.
            return None
        return row["v"] if row and row["v"] is not None else None

    def _apply_schema(self) -> None:
        schema_sql = _load_schema_sql()
        self.conn.executescript(schema_sql)
        self.conn.commit()
        # Vec table lives in its own DDL because it depends on the
        # sqlite-vec extension being loaded (which we can't assume in
        # schema.sql — that file should be parseable by any sqlite).
        self._create_vec_table()

    def _create_vec_table(self) -> None:
        """Create paper_chunks_vec if sqlite-vec is loaded.

        No-op if the extension isn't available — vector queries will
        fail loudly when someone actually calls them, which is a
        better error surface than a cryptic schema error.
        """
        if not self.vec_available:
            log.info(
                "sqlite-vec not loaded; skipping paper_chunks_vec creation. "
                "Vector search will be unavailable."
            )
            return
        # vec0 doesn't support IF NOT EXISTS, so check existence first.
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='paper_chunks_vec'"
        ).fetchone()
        if row:
            return
        # Dimension comes from Store(vec_dim=...) which in turn comes
        # from config.embedding_dim. MUST match what the embedding
        # provider produces — sqlite-vec will error on insert if the
        # embedding blob length mismatches. Reindex with the correct
        # --dim after switching providers.
        self.conn.execute(
            f"CREATE VIRTUAL TABLE paper_chunks_vec USING vec0("
            f"    chunk_id INTEGER PRIMARY KEY,"
            f"    embedding FLOAT[{self.vec_dim}]"
            f")"
        )
        self.conn.commit()

    def _stamp_version(self, version: int) -> None:
        from datetime import datetime, timezone
        self.conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def _drop_all(self) -> None:
        """Drop every table we own. Safe to call before re-applying.

        We enumerate explicitly (rather than PRAGMA table_list + drop)
        to avoid dropping user-added tables if any.
        """
        tables = [
            "paper_chunks_vec",       # Phase 2b, vec virtual table
            "paper_chunk_meta",       # Phase 2b
            "paper_fts",
            "links",                  # Phase 2c
            "paper_collections",
            "paper_tags",
            "paper_attachments",
            "thoughts",
            "topics",
            "notes",
            "papers",
            "schema_version",
        ]
        for t in tables:
            # vec0 virtual table DROP requires the extension loaded;
            # if it's not, the table doesn't exist anyway.
            try:
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
            except sqlite3.OperationalError as e:
                log.warning("Could not drop %s: %s (continuing)", t, e)
        self.conn.commit()

    # -----------------------------------------------------------------
    # Convenience accessors
    # -----------------------------------------------------------------

    def commit(self) -> None:
        self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq_of_params) -> sqlite3.Cursor:
        return self.conn.executemany(sql, seq_of_params)


def default_db_path(kb_root: Path) -> Path:
    """Conventional location of the projection DB inside a KB."""
    return kb_root / ".kb-mcp" / "index.sqlite"


def get_connection(kb_root: Path) -> sqlite3.Connection:
    """Return a raw `sqlite3.Connection` to the KB's projection DB.

    Convenience for external packages (kb_citations, future
    analyzers) that want to poke the DB without constructing a full
    Store with schema-ensure side-effects. Connection has row_factory
    set to sqlite3.Row so consumers get dict-style access.

    Does NOT ensure the schema exists — caller must have run
    `kb-mcp index` at least once.

    Raises FileNotFoundError if the DB file is missing. We check
    explicitly because sqlite3.connect() otherwise creates an empty
    file silently — external callers would then think they have
    a valid DB and get confusing "no such table" errors on every
    query. Fail loudly at connection time instead.
    """
    db_path = default_db_path(kb_root)
    if not db_path.is_file():
        raise FileNotFoundError(
            f"projection DB not found at {db_path}. "
            f"Run `kb-mcp index` first to build it."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _load_schema_sql() -> str:
    """Read schema.sql bundled with the package."""
    pkg = resources.files("kb_mcp")
    return (pkg / "schema.sql").read_text(encoding="utf-8")


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension into this connection.

    Returns True on success, False if unavailable (sqlite-vec not
    installed, OS missing extension loading support, etc.). Callers
    should check store.vec_available before executing vec0 queries.
    """
    try:
        import sqlite_vec
    except ImportError:
        log.warning(
            "sqlite-vec not installed. Vector search will be "
            "unavailable. Run `pip install sqlite-vec` to enable."
        )
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except (sqlite3.OperationalError, AttributeError) as e:
        # enable_load_extension may be missing on macOS Python built
        # without extension support, or the load may fail on some
        # platforms. We degrade gracefully.
        log.warning("Could not load sqlite-vec extension: %s", e)
        return False
