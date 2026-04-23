-- kb-mcp projection schema.
--
-- Phase 2a covers the relational projection: papers, notes, topics,
-- thoughts, attachments, tags, and a FTS5 full-text index. Vectors
-- (paper_chunks) and link graph (links) land in phases 2b and 2c.
--
-- Principles:
--  - SQLite file is DERIVED from md files — can always be rebuilt.
--    Deleting the db + running `kb-mcp index` restores consistency.
--  - Every md-backed row tracks md_mtime so incremental reindex is
--    just "file newer than our row?".
--  - All text is UTF-8.

-- Version tracking. store.py reads MAX(version) and rebuilds on
-- mismatch with EXPECTED_SCHEMA_VERSION.
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

-- -------- core md-backed entities --------

CREATE TABLE IF NOT EXISTS papers (
    -- Primary key: the md file's stem (filename without .md).
    -- For regular papers, paper_key == zotero_key, e.g. "ABCD1234".
    -- For book / long-article chapters, paper_key is the md stem
    -- like "BOOKKEY-ch03" (v26+) while zotero_key is the parent
    -- Zotero key "BOOKKEY" — i.e. one Zotero item yields multiple
    -- rows (the whole book at BOOKKEY, each chapter at BOOKKEY-chNN).
    paper_key TEXT PRIMARY KEY,

    -- The Zotero identity shared by a whole-book row and all its
    -- chapter rows. Equals paper_key for single-md papers (the
    -- common case). NOT unique — multiple rows may share it when
    -- a work is split across several mds.
    zotero_key TEXT NOT NULL,

    title TEXT,
    authors TEXT,                      -- JSON array string
    year INTEGER,
    item_type TEXT,
    citation_key TEXT,
    doi TEXT,
    publication TEXT,
    abstract TEXT,
    fulltext_processed INTEGER NOT NULL DEFAULT 0,
    fulltext_source TEXT,
    md_path TEXT NOT NULL,
    md_mtime REAL NOT NULL,
    md_size INTEGER,
    last_indexed_at TEXT NOT NULL,
    -- Phase 2b: embedding state.
    -- embedded=1 means paper_chunks has up-to-date vectors for this
    -- paper; 0 means either never embedded or embedding failed.
    -- When a paper's md changes and we re-index the row, we set this
    -- back to 0 until the embedding step (possibly in a later run)
    -- succeeds. The vec table is kept consistent by DELETE-before-INSERT.
    embedded INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT,              -- e.g. "text-embedding-3-small"
    embedded_at TEXT,
    -- Phase 4: citation count from external providers.
    -- NULL = unknown (never fetched). 0 = known but uncited (valid).
    -- Refreshed by `kb-citations refresh-counts` or implicitly at
    -- fetch time. Source recorded so UI can say e.g. "per OpenAlex".
    -- For multi-md papers (book + chapters), the canonical citation
    -- count lives on the whole-book row (paper_key == zotero_key);
    -- chapter rows may have NULL.
    citation_count INTEGER,
    citation_count_source TEXT,        -- "semantic_scholar" | "openalex"
    citation_count_updated_at TEXT     -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_item_type ON papers(item_type);
CREATE INDEX IF NOT EXISTS idx_papers_processed ON papers(fulltext_processed);
CREATE INDEX IF NOT EXISTS idx_papers_citation_count ON papers(citation_count);
-- v26: lookups by zotero_key (the Zotero identity) now need an
-- index because it's no longer the primary key. Used by any
-- "list all mds for this Zotero item" query.
CREATE INDEX IF NOT EXISTS idx_papers_zotero_key ON papers(zotero_key);

CREATE TABLE IF NOT EXISTS notes (
    zotero_key TEXT PRIMARY KEY,
    title TEXT,
    md_path TEXT NOT NULL,
    md_mtime REAL NOT NULL,
    last_indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    slug TEXT PRIMARY KEY,
    title TEXT,
    md_path TEXT NOT NULL,
    md_mtime REAL NOT NULL,
    last_indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thoughts (
    slug TEXT PRIMARY KEY,
    title TEXT,
    md_path TEXT NOT NULL,
    md_mtime REAL NOT NULL,
    created_at TEXT,
    last_indexed_at TEXT NOT NULL
);

-- -------- relations --------

-- Reverse-lookup: attachment_key → paper_key. The O(1) replacement
-- for the frontmatter grep in kb-importer set-summary's hint logic.
CREATE TABLE IF NOT EXISTS paper_attachments (
    attachment_key TEXT PRIMARY KEY,
    -- v27 fix (was v6 bug): target papers(paper_key), NOT
    -- papers(zotero_key). papers.paper_key is the PK; zotero_key
    -- is a non-unique index column since v6 (book chapters share
    -- a zotero_key across multiple paper_key rows). Targeting
    -- zotero_key in a FK triggers SQLite's "foreign key mismatch"
    -- on every INSERT because SQLite requires FK targets to be
    -- PK or UNIQUE. v6 shipped with this broken across four side
    -- tables; v27 repoints all four to paper_key.
    paper_key TEXT NOT NULL REFERENCES papers(paper_key) ON DELETE CASCADE,
    is_main INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paper_att_paper ON paper_attachments(paper_key);

CREATE TABLE IF NOT EXISTS paper_tags (
    -- v27 fix: see paper_attachments FK comment.
    paper_key TEXT NOT NULL REFERENCES papers(paper_key) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('zotero', 'kb')),
    PRIMARY KEY (paper_key, tag, source)
);
CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags(tag);

CREATE TABLE IF NOT EXISTS paper_collections (
    -- v27 fix: see paper_attachments FK comment.
    paper_key TEXT NOT NULL REFERENCES papers(paper_key) ON DELETE CASCADE,
    collection_name TEXT NOT NULL,
    PRIMARY KEY (paper_key, collection_name)
);
CREATE INDEX IF NOT EXISTS idx_paper_coll_name ON paper_collections(collection_name);

-- -------- full-text search (FTS5) --------

-- Rebuilt explicitly on each paper index; no triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
    paper_key UNINDEXED,
    title,
    authors,
    abstract,
    fulltext,
    tokenize='unicode61 remove_diacritics 2'
);

-- -------- vector search (sqlite-vec, Phase 2b) --------

-- Metadata about each chunk: what it is, where in the paper, text
-- content (for snippet display). The vec virtual table holds just
-- the embedding; we JOIN on chunk_id for text + metadata.
CREATE TABLE IF NOT EXISTS paper_chunk_meta (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- v27 fix: see paper_attachments FK comment.
    paper_key TEXT NOT NULL REFERENCES papers(paper_key) ON DELETE CASCADE,
    kind TEXT NOT NULL,            -- "header" | "section"
    section_num INTEGER,           -- 1..7 for section chunks, NULL for header
    section_title TEXT,            -- e.g. "1. 论文的主要内容" or NULL
    text TEXT NOT NULL             -- actual chunk text (for display)
);
CREATE INDEX IF NOT EXISTS idx_chunk_meta_paper ON paper_chunk_meta(paper_key);

-- NOTE: paper_chunks_vec (the sqlite-vec virtual table) is created
-- separately in store.py's ensure_schema, because it requires the
-- sqlite-vec extension to be loaded. If the extension is unavailable
-- the main schema still works; only vector queries will fail.

-- -------- link graph (Phase 2c) --------

-- Bidirectional-lookup edge table. Each row is one outgoing edge
-- from a source node to a destination node. backlinks(X) is just
-- WHERE dst_type=? AND dst_key=?; outgoing is WHERE src_*.
--
-- Node types: 'paper', 'note', 'topic', 'thought'.
-- Plus 'dangling' for a destination that didn't match any known
-- node at resolve time (e.g. [[SOMEKEY]] where SOMEKEY doesn't exist
-- yet). Dangling edges stay in the table so resolution is re-tried
-- on the next index run — adding the missing paper later promotes
-- the edge to a real one automatically.
--
-- origin tells us how the edge was discovered:
--   'frontmatter'  — explicit kb_refs: list
--   'wikilink'     — [[slug]] in body
--   'mdlink'       — [text](subdir/slug.md) in body
--   'cite'         — @citation_key in body (papers only)
-- Kept in the PK because the same (src, dst) pair can appear via
-- multiple origins (e.g. frontmatter + wikilink confirm each other).
CREATE TABLE IF NOT EXISTS links (
    src_type TEXT NOT NULL CHECK (src_type IN ('paper', 'note', 'topic', 'thought')),
    src_key TEXT NOT NULL,
    dst_type TEXT NOT NULL CHECK (dst_type IN ('paper', 'note', 'topic', 'thought', 'dangling')),
    dst_key TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('frontmatter', 'wikilink', 'mdlink', 'cite', 'citation')),
    PRIMARY KEY (src_type, src_key, dst_type, dst_key, origin)
);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_type, dst_key);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_type, src_key);
