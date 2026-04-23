"""Phase 2b embedding pass — chunks papers + pushes vectors to
sqlite-vec.

Extracted from indexer.py in v0.28.0. Logic unchanged; called via
`Indexer._run_embedding_pass` which is now a thin delegate to
`run_embedding_pass(indexer, report)` here. All store/embedder
state lives on the Indexer, so the free functions accept the
Indexer instance as their first arg.
"""
from __future__ import annotations

import logging

from ._indexer_helpers import (
    _authors_flat, _clamp, _extract_fulltext_body,
    _now_iso, _split_fulltext_sections, _strip_frontmatter, _vec_blob,
)


log = logging.getLogger(__name__)


def run_embedding_pass(indexer, report) -> None:
    """Batch-embed all papers queued during this index run.

    Deduplicates (same paper indexed twice in one run would be a
    bug, but be defensive). Skips silently if no provider or vec
    unavailable. Per-paper failures are tolerated: the paper keeps
    embedded=0 so next run can retry.
    """
    if not indexer._pending_embed:
        return

    # Dedupe, preserve order.
    seen: set[str] = set()
    pending = [
        k for k in indexer._pending_embed
        if k not in seen and not seen.add(k)
    ]
    indexer._pending_embed = []

    if indexer._embedder is None:
        report.embed_skipped = len(pending)
        log.info(
            "Skipping embedding pass for %d paper(s): "
            "no embedding provider configured.", len(pending),
        )
        return

    if not indexer.store.vec_available:
        report.embed_skipped = len(pending)
        log.warning(
            "Skipping embedding pass for %d paper(s): "
            "sqlite-vec extension not loaded.", len(pending),
        )
        return

    log.info(
        "Embedding %d paper(s) with model %s ...",
        len(pending), indexer._embedder.model_name,
    )

    # Gather chunks from all papers first, then batch.
    # Struct: list of (paper_key, chunk_meta_tuple, text)
    # chunk_meta_tuple = (kind, section_num, section_title)
    #
    # v0.27.10: also track expected_per_paper so we can tell
    # "paper P was fully embedded" (inserted == expected) from
    # "paper P was partially embedded" (inserted < expected
    # because some batch failed mid-stream). The pre-0.27.10
    # code flagged ANY paper with at least one successful
    # chunk as embedded=1, which left papers in a
    # DB-says-embedded-but-chunks-are-incomplete state that
    # future reindexes wouldn't retry (embedded=1 path skips
    # queueing).
    all_chunks: list[tuple[str, tuple, str]] = []
    expected_per_paper: dict[str, int] = {}
    for pk in pending:
        try:
            # Call the bound method so tests can monkey-patch
            # indexer._chunk_paper. The default delegates back to
            # chunk_paper() below.
            chunks = indexer._chunk_paper(pk)
        except Exception as e:
            log.warning("Could not chunk paper %s: %s", pk, e)
            report.embed_failed += 1
            continue
        n = 0
        for meta, text in chunks:
            all_chunks.append((pk, meta, text))
            n += 1
        if n:
            expected_per_paper[pk] = n

    if not all_chunks:
        log.info("No embeddable content across %d paper(s).", len(pending))
        return

    # Flush old chunk rows for all pending papers in one shot.
    placeholders = ",".join("?" * len(pending))
    old_ids = [
        r["chunk_id"] for r in indexer.store.execute(
            f"SELECT chunk_id FROM paper_chunk_meta "
            f"WHERE paper_key IN ({placeholders})",
            tuple(pending),
        ).fetchall()
    ]
    indexer.store.execute(
        f"DELETE FROM paper_chunk_meta WHERE paper_key IN ({placeholders})",
        tuple(pending),
    )
    if old_ids:
        cid_ph = ",".join("?" * len(old_ids))
        indexer.store.execute(
            f"DELETE FROM paper_chunks_vec WHERE chunk_id IN ({cid_ph})",
            tuple(old_ids),
        )

    # Call API in batches. Track per-paper inserted count so we
    # can detect partials after the loop.
    inserted_per_paper: dict[str, int] = {}
    for batch_start in range(0, len(all_chunks), indexer._batch_size):
        batch = all_chunks[batch_start:batch_start + indexer._batch_size]
        texts = [t for (_pk, _meta, t) in batch]
        try:
            result = indexer._embedder.embed(texts)
        except Exception as e:
            # One batch failed. Continue with other batches
            # rather than aborting the whole pass — but the
            # paper-completeness check after the loop will
            # catch any paper whose chunks straddled this
            # failed batch and prevent a false embedded=1.
            log.warning(
                "Embedding batch failed (%d texts): %s",
                len(texts), e,
            )
            continue

        report.embed_api_calls += 1
        report.embed_tokens += result.prompt_tokens

        # Insert chunk_meta rows (autoincrement gives chunk_id),
        # then the corresponding vec rows.
        for (pk, meta, text), vec in zip(batch, result.vectors):
            kind, section_num, section_title = meta
            cur = indexer.store.execute(
                "INSERT INTO paper_chunk_meta "
                "(paper_key, kind, section_num, section_title, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (pk, kind, section_num, section_title, text),
            )
            chunk_id = cur.lastrowid
            indexer.store.execute(
                "INSERT INTO paper_chunks_vec (chunk_id, embedding) "
                "VALUES (?, ?)",
                (chunk_id, _vec_blob(vec)),
            )
            inserted_per_paper[pk] = inserted_per_paper.get(pk, 0) + 1
            report.embedded_chunks += 1

    # v0.27.10: separate "fully embedded" from "partially
    # embedded". Only the former gets embedded=1.
    fully_embedded: list[str] = []
    partially_embedded: list[str] = []
    for pk, expected in expected_per_paper.items():
        inserted = inserted_per_paper.get(pk, 0)
        if inserted == expected:
            fully_embedded.append(pk)
        else:
            # This paper straddled a failed batch. Count it as
            # an embed failure so reports are accurate.
            report.embed_failed += 1
            if inserted > 0:
                partially_embedded.append(pk)

    # Delete the partial rows so the papers/paper_chunk_meta
    # state stays coherent (no half-present papers). The
    # paper's md_mtime row itself is left alone — embedded=0,
    # and the _index_paper mtime-unchanged branch will
    # re-queue it for embedding on the next reindex run.
    if partially_embedded:
        log.warning(
            "Partial embedding (will be retried next run): "
            "%d paper(s) had chunks in a failed batch — "
            "cleaning their partial rows.",
            len(partially_embedded),
        )
        pp_ph = ",".join("?" * len(partially_embedded))
        partial_chunk_ids = [
            r["chunk_id"] for r in indexer.store.execute(
                f"SELECT chunk_id FROM paper_chunk_meta "
                f"WHERE paper_key IN ({pp_ph})",
                tuple(partially_embedded),
            ).fetchall()
        ]
        indexer.store.execute(
            f"DELETE FROM paper_chunk_meta "
            f"WHERE paper_key IN ({pp_ph})",
            tuple(partially_embedded),
        )
        if partial_chunk_ids and indexer.store.vec_available:
            cid_ph = ",".join("?" * len(partial_chunk_ids))
            indexer.store.execute(
                f"DELETE FROM paper_chunks_vec "
                f"WHERE chunk_id IN ({cid_ph})",
                tuple(partial_chunk_ids),
            )
        # Also discount the chunks we just deleted from the
        # reported total (they aren't really "embedded").
        report.embedded_chunks -= sum(
            inserted_per_paper.get(pk, 0) for pk in partially_embedded
        )

    # Mark successfully-embedded papers in the papers table.
    # v26: the PK is paper_key (md stem), matching what we pushed
    # onto _pending_embed and what survived the embedding call.
    if fully_embedded:
        placeholders = ",".join("?" * len(fully_embedded))
        indexer.store.execute(
            f"UPDATE papers SET embedded = 1, "
            f"embedding_model = ?, embedded_at = ? "
            f"WHERE paper_key IN ({placeholders})",
            (indexer._embedder.model_name, _now_iso(), *fully_embedded),
        )
    report.embedded_papers = len(fully_embedded)
    indexer.store.commit()
    log.info(
        "Embedded %d papers → %d chunks in %d API call(s), %d tokens total.",
        report.embedded_papers, report.embedded_chunks,
        report.embed_api_calls, report.embed_tokens,
    )


def chunk_paper(indexer, paper_key: str) -> list[tuple[tuple, str]]:
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
    row = indexer.store.execute(
        "SELECT title, authors, abstract, md_path FROM papers "
        "WHERE paper_key = ?",
        (paper_key,)
    ).fetchone()
    if row is None:
        return []

    md_full = (indexer.kb_root / row["md_path"]).read_text(encoding="utf-8")
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
