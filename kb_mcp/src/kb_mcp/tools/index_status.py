"""index_status: diagnostic tool for the kb-mcp projection DB."""
from __future__ import annotations

from pathlib import Path

from ..store import Store


def index_status_impl(
    store: Store,
    kb_root: Path,
    *,
    deep: bool = False,
) -> str:
    """Report the state of the projection DB and how it compares to
    the filesystem.

    Useful to diagnose "why didn't my paper show up in search?" — the
    answer is usually "the index is stale, run kb-mcp index".

    When `deep=True` is set (v25+), additionally runs
    `PRAGMA integrity_check` against the SQLite file. This scans
    every DB page and verifies its checksums, catching corruption
    that ordinary SELECT queries miss — single-byte flips from
    filesystem errors or partial writes can leave the index
    readable-but-wrong, and only a full scan surfaces them. Slow
    (scales with DB size), so only run on-demand, not in routine
    status checks.
    """
    lines = [f"kb_root: {kb_root}", f"db_path: {store.db_path}", ""]

    # Counts per table.
    for table, label in [
        ("papers", "Papers"),
        ("notes", "Standalone notes"),
        ("topics", "Topics"),
        ("thoughts", "Thoughts"),
        ("paper_attachments", "Paper attachments"),
    ]:
        n = store.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        lines.append(f"  {label}: {n}")

    # How many papers have fulltext summaries?
    n_sum = store.execute(
        "SELECT COUNT(*) AS c FROM papers WHERE fulltext_processed = 1"
    ).fetchone()["c"]
    n_pap = store.execute("SELECT COUNT(*) AS c FROM papers").fetchone()["c"]
    if n_pap:
        pct = 100 * n_sum / n_pap
        lines.append(f"  Papers with AI summary: {n_sum}/{n_pap} ({pct:.0f}%)")

    # RAG quality: how many papers are indexed with more than a header
    # chunk? A header-only paper has ~1 chunk (title + abstract);
    # a summary-backed paper has 5–10+ chunks. Low ratio → RAG will
    # mostly retrieve titles, not substance. Run kb-importer with
    # --fulltext to fix.
    try:
        rows = store.execute(
            """
            SELECT p.paper_key, COUNT(pcm.chunk_id) AS n
              FROM papers p
              LEFT JOIN paper_chunk_meta pcm
                ON pcm.paper_key = p.paper_key
             GROUP BY p.paper_key
            """
        ).fetchall()
        n_header = sum(1 for r in rows if r["n"] <= 1)
        if n_pap:
            lines.append(
                f"  Papers indexed as header-only: {n_header}/{n_pap} "
                f"({100 * n_header / n_pap:.0f}%) "
                f"— these have ≤1 chunk"
            )
    except Exception:
        pass  # paper_chunk_meta may not exist if embeddings disabled

    # Citation count populated? (Phase 4; requires kb-citations refresh-counts)
    try:
        n_cc = store.execute(
            "SELECT COUNT(*) AS c FROM papers "
            "WHERE citation_count IS NOT NULL"
        ).fetchone()["c"]
        if n_pap:
            lines.append(
                f"  Papers with citation_count: {n_cc}/{n_pap} "
                f"({100 * n_cc / n_pap:.0f}%)"
            )
    except Exception:
        pass

    lines.append("")

    # Staleness check: count md files on disk vs. DB rows.
    stale = _count_stale(store, kb_root)
    lines.append(f"Staleness (md mtime ahead of DB):")
    lines.append(f"  Papers:   {stale['papers']}")
    lines.append(f"  Notes:    {stale['notes']}")
    lines.append(f"  Topics:   {stale['topics']}")
    lines.append(f"  Thoughts: {stale['thoughts']}")
    lines.append(f"  Missing (md on disk not in DB): {stale['missing']}")
    lines.append(f"  Orphan (in DB but no md):       {stale['orphan']}")

    if stale["total_issues"] > 0:
        lines.append("")
        lines.append("→ Run `kb-mcp index` to refresh.")

    # Parse-error scan: try to load frontmatter from every md file and
    # count failures. Malformed YAML silently skips that paper at
    # index time — users used to not notice until search results had
    # missing papers. This surface the count here + lists the first
    # few offenders.
    parse_errors = _scan_parse_errors(kb_root)
    if parse_errors:
        lines.append("")
        lines.append(f"⚠ YAML parse errors: {len(parse_errors)}")
        for rel, msg in parse_errors[:5]:
            lines.append(f"  {rel}: {msg}")
        if len(parse_errors) > 5:
            lines.append(f"  ... +{len(parse_errors) - 5} more")
        lines.append("  (these papers are skipped by the indexer)")

    # Legacy-format scan: papers that claim fulltext_processed=true
    # but have no <!-- kb-fulltext-start/end --> markers. These come
    # from pre-v21 versions of the importer that wrote summaries
    # directly into `post.content` without wrapping markers. The FTS
    # / vec index can't extract their fulltext region (returns empty),
    # so they're effectively retrieval-dark until regenerated.
    #
    # v21+ has a self-heal: the NEXT metadata re-import or
    # --force-fulltext run will synthesize markers around the
    # existing body. No data loss, but the papers are less useful
    # for search until that happens.
    legacy = _scan_legacy_markers(kb_root)
    if legacy:
        lines.append("")
        lines.append(
            f"ⓘ Pre-v21 format (fulltext_processed=true, no markers): "
            f"{len(legacy)}"
        )
        for rel in legacy[:5]:
            lines.append(f"  {rel}")
        if len(legacy) > 5:
            lines.append(f"  ... +{len(legacy) - 5} more")
        lines.append(
            "  These will self-heal on next metadata re-import "
            "(no LLM cost). To fix now:"
        )
        lines.append(
            "    kb-importer import papers --all-pending"
        )

    # v26: report deprecated v25 directory paths. Content at these
    # locations is no longer indexed — the user needs to reorganise
    # (or delete) these files per the v26 layout.
    deprecated = _scan_deprecated_v25_paths(kb_root)
    if deprecated:
        lines.append("")
        lines.append(
            f"⚠  v25 legacy paths (NOT indexed in v26): "
            f"{sum(len(v) for v in deprecated.values())} file(s) "
            f"need reorganising:"
        )
        if deprecated.get("zotero-notes"):
            n = len(deprecated["zotero-notes"])
            lines.append(
                f"  - {n} file(s) under zotero-notes/ → move to "
                f"topics/standalone-note/"
            )
            for rel in deprecated["zotero-notes"][:3]:
                lines.append(f"      {rel}")
            if n > 3:
                lines.append(f"      ... +{n - 3} more")
        if deprecated.get("top-topics"):
            n = len(deprecated["top-topics"])
            lines.append(
                f"  - {n} file(s) at top-level topics/*.md → move to "
                f"topics/agent-created/"
            )
            for rel in deprecated["top-topics"][:3]:
                lines.append(f"      {rel}")
            if n > 3:
                lines.append(f"      ... +{n - 3} more")
        if deprecated.get("book-chapter-thoughts"):
            n = len(deprecated["book-chapter-thoughts"])
            lines.append(
                f"  - {n} thought(s) matching book-chapter pattern "
                f"(thoughts/<date>-<KEY>-ch<NN>-*.md) → these are v25 "
                f"book chapters; in v26 they should be consolidated as "
                f"papers/<KEY>-chNN.md"
            )
            for rel in deprecated["book-chapter-thoughts"][:3]:
                lines.append(f"      {rel}")
            if n > 3:
                lines.append(f"      ... +{n - 3} more")
        lines.append(
            "  Content at deprecated paths is considered obsolete; "
            "move, rewrite, or delete per the v26 layout. See "
            "README.md for the new directory map."
        )

    # Deep integrity check (v25+) — only when explicitly requested.
    # Catches bit-rot / partial writes that don't surface in normal
    # queries. Runs PRAGMA integrity_check which scans every page
    # and verifies page-level structure; finds issues like "invalid
    # page number", "wrong # of entries", etc. Reports up to 100
    # errors (PRAGMA default) — if the DB is seriously corrupt,
    # that limit is irrelevant (you need to rebuild anyway).
    if deep:
        lines.append("")
        lines.append("Deep integrity check (PRAGMA integrity_check):")
        try:
            rows = store.execute(
                "PRAGMA integrity_check"
            ).fetchall()
            messages = [r[0] if isinstance(r, tuple) else r["integrity_check"]
                        for r in rows]
            if len(messages) == 1 and messages[0] == "ok":
                lines.append("  ✓ DB file passed SQLite integrity check.")
            else:
                lines.append(
                    f"  ✗ FAIL — {len(messages)} issue(s) reported by "
                    f"SQLite. First few:"
                )
                for m in messages[:10]:
                    lines.append(f"    {m}")
                if len(messages) > 10:
                    lines.append(f"    ... +{len(messages) - 10} more")
                lines.append(
                    "  DB corruption detected. Recommended: "
                    "`kb-mcp index --force` to rebuild from md "
                    "(safe — projection DB is derived from md, "
                    "never the source of truth)."
                )
        except Exception as e:
            lines.append(f"  ⚠  integrity_check could not run: {e}")

    return "\n".join(lines)


def _scan_parse_errors(kb_root: Path) -> list[tuple[str, str]]:
    """Try frontmatter.load on every md; return [(rel_path, err_msg), ...]
    for files that don't parse cleanly. Fast-fail: stops trying to
    parse after first raise per file.
    """
    import frontmatter  # local to avoid dep at module top
    from ..paths import (
        PAPERS_DIR, TOPICS_STANDALONE_DIR,
        TOPICS_AGENT_DIR, THOUGHTS_DIR,
    )
    errors: list[tuple[str, str]] = []
    # v26 active subdirs. The rglob happens inside topics/agent-created
    # because that bucket alone may have nested hierarchy; the others
    # are flat.
    for subdir, is_nested in (
        (PAPERS_DIR,            False),
        (TOPICS_STANDALONE_DIR, False),
        (TOPICS_AGENT_DIR,      True),
        (THOUGHTS_DIR,          False),
    ):
        d = kb_root / subdir
        if not d.exists():
            continue
        glob = d.rglob("*.md") if is_nested else d.glob("*.md")
        for md in glob:
            if md.name.startswith("."):
                continue
            try:
                frontmatter.load(str(md))
            except Exception as e:
                rel = md.relative_to(kb_root).as_posix()
                # Keep the message short — just the exception summary.
                msg = f"{type(e).__name__}: {str(e)[:80]}"
                errors.append((rel, msg))
    return errors


def _scan_legacy_markers(kb_root: Path) -> list[str]:
    """Return paper mds that have fulltext_processed=true in
    frontmatter but lack the `<!-- kb-fulltext-start -->` marker in
    their body. These are pre-v21 data that need self-heal.

    Cheap scan: reads each paper md once with a simple substring
    check. No frontmatter parsing beyond the header block (mirrors
    the streaming read used elsewhere).
    """
    papers_dir = kb_root / "papers"
    if not papers_dir.is_dir():
        return []

    FT_MARKER = "<!-- kb-fulltext-start -->"
    offenders: list[str] = []
    for md in papers_dir.glob("*.md"):
        if md.name.startswith("."):
            continue
        try:
            # Read up to ~32 KB — enough to see both frontmatter and
            # at least the region where markers would be for a normal
            # paper md. Avoids slurping 500 KB if the file is huge.
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Cheap frontmatter-only fulltext_processed check.
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---\n", 4)
        if end < 0:
            continue
        header = text[4:end]
        # Simple substring match — avoids YAML parse cost on 1000 mds.
        # Accept: `fulltext_processed: true`, `: "true"`, `: 'true'`,
        # YAML-truthy alternatives yes/on/1. Stripping quotes is
        # deliberately cheap — we don't need a full YAML parser here.
        has_flag = False
        for line in header.splitlines():
            s = line.strip()
            if s.startswith("fulltext_processed:"):
                val = s.split(":", 1)[1].strip()
                # Strip one layer of matching quotes.
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if val.lower() in ("true", "yes", "on", "1"):
                    has_flag = True
                break
        if not has_flag:
            continue
        if FT_MARKER in text:
            continue
        offenders.append(md.relative_to(kb_root).as_posix())
    return offenders


def _count_stale(store: Store, kb_root: Path) -> dict:
    """Scan md files and compare mtimes vs. DB. O(N files + N rows).

    Doesn't modify the DB; purely observational.
    """
    result = {"papers": 0, "notes": 0, "topics": 0, "thoughts": 0,
              "missing": 0, "orphan": 0, "total_issues": 0}

    # --- Build a lookup of DB rows ---
    db_paths: dict[str, float] = {}  # md_path -> md_mtime
    for table in ("papers", "notes", "topics", "thoughts"):
        rows = store.execute(
            f"SELECT md_path, md_mtime FROM {table}"
        ).fetchall()
        for r in rows:
            db_paths[r["md_path"]] = r["md_mtime"]

    # --- Walk the filesystem (v26 active subdirs) ---
    from ..paths import (
        PAPERS_DIR, TOPICS_STANDALONE_DIR,
        TOPICS_AGENT_DIR, THOUGHTS_DIR,
    )
    disk_paths: set[str] = set()
    for subdir, table_name, is_nested in [
        (PAPERS_DIR,            "papers",   False),
        (TOPICS_STANDALONE_DIR, "notes",    False),
        (TOPICS_AGENT_DIR,      "topics",   True),
        (THOUGHTS_DIR,          "thoughts", False),
    ]:
        d = kb_root / subdir
        if not d.exists():
            continue
        glob = d.rglob("*.md") if is_nested else d.glob("*.md")
        for md in glob:
            if md.name.startswith("."):
                continue
            rel = md.relative_to(kb_root).as_posix()
            disk_paths.add(rel)
            mtime = md.stat().st_mtime
            db_mtime = db_paths.get(rel)
            if db_mtime is None:
                result["missing"] += 1
            elif mtime > db_mtime + 1e-6:
                result[table_name] += 1

    # Orphans: in DB but no md file.
    result["orphan"] = sum(1 for p in db_paths if p not in disk_paths)

    result["total_issues"] = (
        result["papers"] + result["notes"] + result["topics"]
        + result["thoughts"] + result["missing"] + result["orphan"]
    )
    return result


def _scan_deprecated_v25_paths(kb_root: Path) -> dict[str, list[str]]:
    """Find files at v25 locations that v26 no longer indexes.

    Returns a dict with three keys (each mapping to a list of
    kb-relative paths; absent key if empty):

      - "zotero-notes":           *.md directly under zotero-notes/
      - "top-topics":              *.md at topics/ top level (not
                                   under standalone-note/ or
                                   agent-created/ sub-buckets)
      - "book-chapter-thoughts":   thoughts/*-<KEY>-ch<NN>-*.md
                                   (v25 book chapter convention;
                                   in v26 these should be under
                                   papers/<KEY>-chNN.md instead)

    NOT auto-migrated — the user must reorganise explicitly (per
    the v26 design decision). This scan is diagnostic only.
    """
    import re as _re
    out: dict[str, list[str]] = {}

    # 1. zotero-notes/*.md
    zn = kb_root / "zotero-notes"
    if zn.is_dir():
        hits = sorted(
            md.relative_to(kb_root).as_posix()
            for md in zn.glob("*.md")
            if not md.name.startswith(".")
        )
        if hits:
            out["zotero-notes"] = hits

    # 2. Top-level topics/*.md (not in a sub-bucket).
    topics_top = kb_root / "topics"
    if topics_top.is_dir():
        hits = []
        for md in topics_top.glob("*.md"):  # direct children, not rglob
            if md.name.startswith("."):
                continue
            hits.append(md.relative_to(kb_root).as_posix())
        if hits:
            out["top-topics"] = sorted(hits)

    # 3. thoughts/ book-chapter naming pattern: `<date>-<key>-chNN-*.md`.
    # This is the v25 longform output; in v26 those chapters move to
    # papers/<KEY>-chNN.md.
    th = kb_root / "thoughts"
    if th.is_dir():
        # Match `YYYY-MM-DD-<anything>-ch<digits>-<slug>.md` (v24 lowercase
        # form) or `-ch<digits>` with or without slug suffix.
        ch_re = _re.compile(
            r"^\d{4}-\d{2}-\d{2}-[^-]+.*-ch\d+.*\.md$",
            _re.IGNORECASE,
        )
        hits = sorted(
            md.relative_to(kb_root).as_posix()
            for md in th.glob("*.md")
            if not md.name.startswith(".") and ch_re.match(md.name)
        )
        if hits:
            out["book-chapter-thoughts"] = hits

    return out
