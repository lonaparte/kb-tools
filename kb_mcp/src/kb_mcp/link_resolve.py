"""Phase 2c link-graph resolution for the indexer.

Extracted from indexer.py in v0.28.0. Two phases:

- `stage_refs` called per-md during the main scan: pulls kb_refs
  + wikilinks + @cite candidates from each md and queues them on
  the Indexer's `_staged_links` buffer.
- `resolve_staged_links` runs after ALL mds have been scanned:
  each candidate is classified into a typed edge (paper/note/
  topic/thought) or 'dangling' if the target doesn't exist yet,
  then batch-inserted.

Deferring resolution lets us handle forward references (a thought
referencing a paper whose md we reach later in the walk). `_resolve_one`
and `_exists` are pure helpers without Indexer state.
"""
from __future__ import annotations

import logging

from .link_extractor import ExtractedRef, extract_refs


log = logging.getLogger(__name__)


def stage_refs(
    indexer,
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
    indexer._touched_srcs.add((src_type, src_key))
    for ref in refs:
        indexer._staged_links.append((src_type, src_key, ref))


def resolve_staged_links(indexer, report) -> None:
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
      5. v0.28.2 dangling-promotion pass (NEW): iterate existing
         rows where dst_type='dangling' and retry resolution
         against the current node tables. Any that now resolve get
         their dst_type / dst_key updated in-place. This closes
         the G18 gap: before 0.28.2, a dangling edge survived
         until the SRC md's mtime advanced, because the incremental
         indexer only re-stages refs for touched srcs. Now the
         index pass ALWAYS sweeps dangling edges, so users who
         import paper B see A's edge to B promote on the next
         `kb-mcp index` whether or not A was touched.
    """
    if (not indexer._touched_srcs
            and not indexer._staged_links):
        # No new work, but there might still be old dangling edges
        # whose targets landed since the previous run. Run the
        # promotion pass and return.
        _promote_dangling_edges(indexer, report)
        return

    # 1. Purge old edges for touched srcs.
    if indexer._touched_srcs:
        # Chunked delete to avoid SQL parameter limits (~999).
        srcs = list(indexer._touched_srcs)
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
            indexer.store.execute(
                f"DELETE FROM links WHERE ({placeholders}) "
                f"AND origin != 'citation'",
                tuple(params),
            )

    if not indexer._staged_links:
        indexer.store.commit()
        # Even with nothing staged, a new node may have landed that
        # promotes a previously-dangling edge (the common case:
        # paper B was imported, A pointed at it dangling, A's
        # mtime didn't advance so its refs weren't re-staged).
        _promote_dangling_edges(indexer, report)
        return

    # 2. Lookup maps. For each node type we just need "does X
    # exist?". citation_key gets a dedicated map for @cite refs.
    # v26: papers keyed by paper_key (md stem) because that's
    # what kb_refs addresses resolve to (kb_refs values look
    # like "papers/BOOKKEY-ch03", whose tail is the paper_key).
    paper_keys = {r["paper_key"] for r in indexer.store.execute(
        "SELECT paper_key FROM papers"
    ).fetchall()}
    note_keys = {r["zotero_key"] for r in indexer.store.execute(
        "SELECT zotero_key FROM notes"
    ).fetchall()}
    topic_slugs = {r["slug"] for r in indexer.store.execute(
        "SELECT slug FROM topics"
    ).fetchall()}
    thought_slugs = {r["slug"] for r in indexer.store.execute(
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
        for r in indexer.store.execute(
            "SELECT citation_key, paper_key FROM papers "
            "WHERE citation_key IS NOT NULL AND citation_key != '' "
            "AND paper_key = zotero_key"
        ).fetchall()
    }

    # 3 + 4. Resolve and batch insert.
    rows: list[tuple] = []
    dangling_count = 0
    seen: set[tuple] = set()
    for src_type, src_key, ref in indexer._staged_links:
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
        indexer.store.executemany(
            "INSERT OR IGNORE INTO links "
            "(src_type, src_key, dst_type, dst_key, origin) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    indexer.store.commit()

    report.links_written = len(rows)
    report.links_dangling = dangling_count
    log.info(
        "Resolved %d link edge(s), %d dangling.",
        len(rows), dangling_count,
    )

    # Clear state so another index_all() call starts fresh.
    indexer._staged_links = []
    indexer._touched_srcs = set()

    # Dangling-promotion pass using the same lookup maps we just
    # built. Cheap: one SELECT + per-row set lookup.
    _promote_dangling_edges(
        indexer, report,
        paper_keys=paper_keys, note_keys=note_keys,
        topic_slugs=topic_slugs, thought_slugs=thought_slugs,
        citation_to_paper=citation_to_paper,
    )


def _promote_dangling_edges(
    indexer, report,
    *,
    paper_keys: set[str] | None = None,
    note_keys: set[str] | None = None,
    topic_slugs: set[str] | None = None,
    thought_slugs: set[str] | None = None,
    citation_to_paper: dict[str, str] | None = None,
) -> None:
    """Scan the links table for rows with dst_type='dangling' and
    re-resolve each against the current node tables. Promote in-place
    when a target now exists.

    Called from two places:
      - at the end of resolve_staged_links' main work (with the
        already-built lookup maps passed in — no extra SELECTs), and
      - from the early-return branch when nothing was staged (builds
        its own maps; typical "idle re-index" path).

    No-op when there are no dangling edges.
    """
    # Cheap cardinality probe before doing real work.
    n_dangling = indexer.store.execute(
        "SELECT COUNT(*) FROM links WHERE dst_type = 'dangling'"
    ).fetchone()[0]
    if n_dangling == 0:
        return

    # Build lookup maps only if the caller didn't pass them in.
    if paper_keys is None:
        paper_keys = {r["paper_key"] for r in indexer.store.execute(
            "SELECT paper_key FROM papers"
        ).fetchall()}
    if note_keys is None:
        note_keys = {r["zotero_key"] for r in indexer.store.execute(
            "SELECT zotero_key FROM notes"
        ).fetchall()}
    if topic_slugs is None:
        topic_slugs = {r["slug"] for r in indexer.store.execute(
            "SELECT slug FROM topics"
        ).fetchall()}
    if thought_slugs is None:
        thought_slugs = {r["slug"] for r in indexer.store.execute(
            "SELECT slug FROM thoughts"
        ).fetchall()}
    if citation_to_paper is None:
        citation_to_paper = {
            r["citation_key"]: r["paper_key"]
            for r in indexer.store.execute(
                "SELECT citation_key, paper_key FROM papers "
                "WHERE citation_key IS NOT NULL AND citation_key != '' "
                "AND paper_key = zotero_key"
            ).fetchall()
        }

    # Pull dangling rows. Small table (dangling edges should be the
    # exception, not the rule) so we load fully.
    rows = list(indexer.store.execute(
        "SELECT src_type, src_key, dst_key, origin FROM links "
        "WHERE dst_type = 'dangling'"
    ).fetchall())

    promoted = 0
    # Process in batches: each row either resolves (UPDATE) or stays
    # dangling (no-op). We DELETE the old row then INSERT the new
    # one because links's PK includes dst_type — UPDATE dst_type
    # would violate the old PK. Use INSERT OR IGNORE for safety in
    # case the promoted edge already exists (race with a concurrent
    # run? defense in depth).
    for row in rows:
        src_type, src_key, dst_key, origin = (
            row["src_type"], row["src_key"], row["dst_key"], row["origin"]
        )
        new_dst_type: str | None = None

        if origin == "cite":
            # @cite always resolves via citation_key.
            resolved = citation_to_paper.get(dst_key)
            if resolved is not None:
                new_dst_type = "paper"
                dst_key = resolved
        else:
            # Try each node type in order of likelihood.
            # Note: we have no hint_type stored for dangling rows,
            # so this matches _resolve_one's no-hint branch.
            if dst_key in paper_keys:
                new_dst_type = "paper"
            elif dst_key in topic_slugs:
                new_dst_type = "topic"
            elif dst_key in thought_slugs:
                new_dst_type = "thought"
            elif dst_key in note_keys:
                new_dst_type = "note"

        if new_dst_type is None:
            continue  # still dangling

        # Replace the old (dangling) row with the promoted one.
        indexer.store.execute(
            "DELETE FROM links "
            "WHERE src_type = ? AND src_key = ? "
            "AND dst_type = 'dangling' AND dst_key = ? AND origin = ?",
            (src_type, src_key, row["dst_key"], origin),
        )
        indexer.store.execute(
            "INSERT OR IGNORE INTO links "
            "(src_type, src_key, dst_type, dst_key, origin) "
            "VALUES (?, ?, ?, ?, ?)",
            (src_type, src_key, new_dst_type, dst_key, origin),
        )
        promoted += 1

    if promoted:
        indexer.store.commit()
        log.info("Promoted %d dangling edge(s) to real targets.", promoted)
        # Report surface: subtract from dangling count if the field
        # exists. It's OK if the caller already set links_dangling;
        # we just reduce it by the count we just promoted.
        try:
            report.links_dangling = max(
                0, (report.links_dangling or 0) - promoted,
            )
        except AttributeError:
            pass
        # Also expose the promotion count for operators who want to
        # see it in the index summary.
        try:
            report.links_promoted = promoted
        except AttributeError:
            pass


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
