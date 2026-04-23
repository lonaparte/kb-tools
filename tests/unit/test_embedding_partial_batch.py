"""Regression for the v0.27.10 embedding partial-batch bug.

Pre-0.27.10: if a paper's chunks straddled multiple embedding
batches and some middle batch failed, the per-append
`success_papers.add(pk)` would still include that paper (because
earlier batches succeeded). The post-loop `UPDATE papers SET
embedded = 1 WHERE paper_key IN success_papers` then flagged the
paper as fully embedded even though only some chunks landed. The
paper's md_mtime matched the row so future reindexes wouldn't
re-queue it — the RAG coverage silently degraded.

v0.27.10 tracks per-paper expected-vs-inserted chunk counts.
Only papers where `inserted == expected` get embedded=1.
Partially-embedded papers get their chunks scrubbed and stay
embedded=0 so the next reindex retries cleanly."""
from __future__ import annotations

from conftest import skip_if_no_frontmatter, skip_if_no_mcp


class _FakeEmbedResult:
    def __init__(self, n_vectors: int, dim: int = 1536):
        # Deterministic fake vectors (zero-filled).
        self.vectors = [[0.0] * dim for _ in range(n_vectors)]
        self.prompt_tokens = n_vectors * 10


class _BatchFailingEmbedder:
    """Embedder that succeeds on most batches but raises on the
    Nth batch. Mimics the real "API quota exceeded mid-stream"
    scenario."""
    model_name = "fake-embedder@1536"

    def __init__(self, fail_on_batch: int):
        self.fail_on_batch = fail_on_batch
        self._batch_count = 0

    def embed(self, texts):
        self._batch_count += 1
        if self._batch_count == self.fail_on_batch:
            raise RuntimeError(
                f"simulated embed failure on batch "
                f"{self._batch_count}"
            )
        return _FakeEmbedResult(len(texts))


def _make_indexer_with_paper(
    tmp_path, *, n_chunks: int, batch_size: int, fail_on_batch: int,
):
    """Build an Indexer whose chunk-production is stubbed to
    yield `n_chunks` chunks for a single paper. Install a fake
    embedder that fails on a specific batch."""
    from kb_mcp.store import Store
    from kb_mcp.indexer import Indexer

    store = Store(tmp_path / "index.sqlite")
    store.ensure_schema()
    # Insert a paper row directly; we don't exercise the md-scan
    # path, only the embedding pass.
    store.execute(
        "INSERT INTO papers "
        "(paper_key, zotero_key, md_path, md_mtime, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("P1", "P1", "papers/P1.md", 1.0, "2026-04-23T00:00:00Z"),
    )
    store.commit()

    idx = Indexer(
        tmp_path, store,
        embedding_provider=_BatchFailingEmbedder(
            fail_on_batch=fail_on_batch
        ),
        embedding_batch_size=batch_size,
    )
    idx._pending_embed = ["P1"]
    # Stub out _chunk_paper to yield n_chunks with minimal meta.
    def _fake_chunks(pk):
        for i in range(n_chunks):
            yield ("section", i + 1, f"§{i+1}"), f"paper {pk} chunk {i+1} text"
    idx._chunk_paper = _fake_chunks
    return idx, store


def test_partial_batch_does_not_mark_embedded(tmp_path):
    """Paper P has 8 chunks; batch_size=5 → 2 batches. Inject
    failure on batch 2 so P has 5 successful inserts and 3 that
    never landed. Pre-0.27.10 would UPDATE embedded=1 despite the
    partial state; v0.27.10 must leave embedded=0 AND scrub the
    partial chunk rows so next reindex starts clean."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()
    idx, store = _make_indexer_with_paper(
        tmp_path, n_chunks=8, batch_size=5, fail_on_batch=2,
    )
    # Skip the vec-table checks if sqlite-vec isn't loaded; the
    # core assertion (embedded flag + chunk_meta scrub) doesn't
    # need vec0.
    if not store.vec_available:
        # We still want embedded-flag + chunk_meta checks. Run the
        # pass via a tiny shim that no-ops the vec writes.
        original_execute = store.execute
        def safe_execute(sql, params=()):
            # sqlite-vec0 DDL fails silently; executing vec
            # INSERT/DELETE against a non-existent table raises.
            if "paper_chunks_vec" in sql:
                class _Cur:
                    lastrowid = 0
                    def fetchall(self): return []
                    def fetchone(self): return None
                return _Cur()
            return original_execute(sql, params)
        store.execute = safe_execute
    from kb_mcp.indexer import IndexReport
    report = IndexReport()
    idx._run_embedding_pass(report)

    # Paper should NOT be marked embedded.
    row = store.execute(
        "SELECT embedded FROM papers WHERE paper_key = ?", ("P1",)
    ).fetchone()
    assert row is not None
    assert row["embedded"] == 0, (
        "paper with incomplete chunks must stay embedded=0 so "
        "the next reindex retries it (pre-0.27.10 bug: embedded=1 "
        "despite partial state → silent RAG degradation)"
    )

    # Partial chunk_meta rows must have been scrubbed so the
    # next reindex starts clean.
    n_chunk_rows = store.execute(
        "SELECT COUNT(*) AS n FROM paper_chunk_meta WHERE paper_key = ?",
        ("P1",),
    ).fetchone()["n"]
    assert n_chunk_rows == 0, (
        f"partial chunk_meta rows not scrubbed: "
        f"{n_chunk_rows} rows left for a paper whose embedding "
        f"was incomplete. Next reindex would see stale data."
    )

    # Report accounting.
    assert report.embed_failed >= 1


def test_all_chunks_fit_in_one_successful_batch(tmp_path):
    """Paper with N chunks, batch size >= N, batch succeeds →
    embedded=1. Happy-path regression to ensure the v0.27.10
    counting change didn't break normal operation."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()
    idx, store = _make_indexer_with_paper(
        tmp_path, n_chunks=3, batch_size=10, fail_on_batch=99,
    )
    from kb_mcp.indexer import IndexReport
    report = IndexReport()
    idx._run_embedding_pass(report)

    row = store.execute(
        "SELECT embedded FROM papers WHERE paper_key = ?", ("P1",)
    ).fetchone()
    assert row["embedded"] == 1


def test_all_chunks_split_across_batches_all_succeed(tmp_path):
    """N=8 chunks, batch_size=5 → two batches. Both succeed.
    Paper must be marked embedded=1 AND all 8 chunks must be in
    paper_chunk_meta."""
    skip_if_no_mcp()
    skip_if_no_frontmatter()
    idx, store = _make_indexer_with_paper(
        tmp_path, n_chunks=8, batch_size=5, fail_on_batch=99,
    )
    from kb_mcp.indexer import IndexReport
    report = IndexReport()
    idx._run_embedding_pass(report)

    row = store.execute(
        "SELECT embedded FROM papers WHERE paper_key = ?", ("P1",)
    ).fetchone()
    assert row["embedded"] == 1
    n = store.execute(
        "SELECT COUNT(*) AS n FROM paper_chunk_meta WHERE paper_key = ?",
        ("P1",),
    ).fetchone()["n"]
    assert n == 8
