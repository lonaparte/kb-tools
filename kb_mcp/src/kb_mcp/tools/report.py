"""`kb-mcp report` — periodic operational digest.

Aggregates events from `<kb_root>/.kb-mcp/events.jsonl` into a
human-readable digest. Designed as a GENERAL report framework:
the skip aggregation is the first section; future sections (re-read
summary, index drift, citation refresh status, ...) plug in the
same way.

Invocation (CLI):
    kb-mcp report                          # defaults: last 30 days, stdout
    kb-mcp report --days 7                 # last week
    kb-mcp report --since 2026-04-01       # from specific date
    kb-mcp report --sections skip,re_read  # pick sections
    kb-mcp report --out digest.md          # write to file
    kb-mcp report --include-normal         # include already_processed

Invocation (MCP tool): identical signature via `kb_report`.

Section framework (for future expansion):

  Each section is a function (kb_root, start, end, opts) → str.
  Register new sections in SECTION_REGISTRY. CLI --sections controls
  which run; missing name = skip silently; unknown section name =
  warn.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


# --- Soft import: events log lives in kb_importer ---
# kb_mcp does not hard-depend on kb_importer; the report section
# degrades cleanly if kb_importer is not installed.
def _read_events(kb_root: Path, since, until, event_types):
    try:
        from kb_importer.events import read_events
    except ImportError:
        return []
    return read_events(
        kb_root, since=since, until=until, event_types=event_types,
    )


# ---------------------------------------------------------------------
# Section: fulltext-skip aggregation
# ---------------------------------------------------------------------

def section_skip(
    kb_root: Path,
    since: datetime, until: datetime,
    *, include_normal: bool = False,
    top_n: int = 3,
) -> str:
    """Aggregate fulltext_skip events in [since, until] window.

    Output (markdown):

        ## Fulltext skips  (2026-03-24 → 2026-04-23, 30 days)

        Total: 42 skips across 37 papers.

        By category:
        - quota_exhausted: 15  (top: ABCD1234, EFGH5678, IJKL9012, +2 more)
        - pdf_missing:     12  ...
        - llm_bad_request:  8  ...
        - llm_other:        5  ...
        - other:            2  ...

        Last run: 2026-04-23T10:14:32Z (ABCD1234 → quota_exhausted)

    When include_normal=False (default) `already_processed` category
    events are excluded — they're not errors, just "paper's done".
    """
    from kb_importer.events import (
        EVENT_FULLTEXT_SKIP, NORMAL_SKIP_CATEGORIES,
    )

    events = _read_events(
        kb_root, since=since, until=until,
        event_types=[EVENT_FULLTEXT_SKIP],
    )

    # Filter out normal skips unless user asked for them.
    if not include_normal:
        events = [
            e for e in events
            if e.get("category") not in NORMAL_SKIP_CATEGORIES
        ]

    header = _section_header(
        "Fulltext skips", since, until,
        extra_note=("(excludes already_processed — pass --include-normal to see)"
                    if not include_normal else "(includes already_processed)"),
    )

    if not events:
        return header + "\n\nNo skip events in window."

    # Aggregate by category, track top paper_keys per category.
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        cat = e.get("category") or "other"
        by_cat[cat].append(e)

    # Total unique papers (across all categories).
    all_keys = {e.get("paper_key") for e in events if e.get("paper_key")}

    lines = [header, ""]
    lines.append(
        f"Total: {len(events)} skip event(s) across "
        f"{len(all_keys)} paper(s)."
    )
    lines.append("")
    lines.append("By category:")

    # Sort categories by count desc for readability.
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        es = by_cat[cat]
        keys_in_cat = [e.get("paper_key") for e in es if e.get("paper_key")]
        # Most-frequent keys in this category first.
        key_counts = Counter(keys_in_cat)
        top = key_counts.most_common(top_n)
        top_str = ", ".join(f"{k}" for k, _ in top)
        extra = len(key_counts) - len(top)
        extra_str = f", +{extra} more" if extra > 0 else ""
        lines.append(
            f"  - {cat}: {len(es)}  "
            f"(top: {top_str}{extra_str})"
            if top else
            f"  - {cat}: {len(es)}"
        )

    # Most recent event for context.
    latest = events[-1]
    lines.append("")
    lines.append(
        f"Last event: {latest.get('ts')} "
        f"({latest.get('paper_key') or '?'} → "
        f"{latest.get('category') or '?'})"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Section: re-read batch outcomes (populated once re-read runs)
# ---------------------------------------------------------------------

def section_re_read(
    kb_root: Path,
    since: datetime, until: datetime,
    **_opts,
) -> str:
    """Aggregate `re_read` events in the window.

    Current output is minimal — just success/skip counts by
    category + which selector was used. Expands as the re-read
    feature matures.
    """
    from kb_importer.events import EVENT_RE_READ
    events = _read_events(
        kb_root, since=since, until=until,
        event_types=[EVENT_RE_READ],
    )

    header = _section_header("Re-read batches", since, until)
    if not events:
        return header + "\n\nNo re-read runs in window."

    total = len(events)
    by_cat = Counter(e.get("category") or "?" for e in events)
    # Count selector usage (from extra.selector if present).
    selectors: Counter = Counter()
    for e in events:
        sel = (e.get("extra") or {}).get("selector")
        if sel:
            selectors[sel] += 1

    lines = [header, ""]
    lines.append(f"Total: {total} re-read event(s).")
    lines.append("")
    lines.append("By outcome:")
    for cat, n in by_cat.most_common():
        lines.append(f"  - {cat}: {n}")
    if selectors:
        lines.append("")
        lines.append("Selectors used:")
        for sel, n in selectors.most_common():
            lines.append(f"  - {sel}: {n} paper(s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Section: re-summarize single-paper outcomes
# ---------------------------------------------------------------------

def section_re_summarize(
    kb_root: Path,
    since: datetime, until: datetime,
    **_opts,
) -> str:
    """Aggregate `re_summarize` events in the window.

    Mirrors the re_read section, but for the single-paper
    `kb-write re-summarize` command. Distinguishing the two lets a
    user see "I ran re-summarize on N papers this month" separately
    from "re-read picked M papers via a selector".
    """
    from kb_importer.events import EVENT_RE_SUMMARIZE
    events = _read_events(
        kb_root, since=since, until=until,
        event_types=[EVENT_RE_SUMMARIZE],
    )

    header = _section_header("Re-summarize (single paper)", since, until)
    if not events:
        return header + "\n\nNo re-summarize runs in window."

    total = len(events)
    by_cat = Counter(e.get("category") or "?" for e in events)
    # Top paper_keys by frequency (same paper may be re-summarized
    # multiple times as the user iterates on a bad summary).
    paper_freq = Counter(
        e.get("paper_key") for e in events if e.get("paper_key")
    )

    lines = [header, ""]
    lines.append(f"Total: {total} re-summarize event(s).")
    lines.append("")
    lines.append("By outcome:")
    for cat, n in by_cat.most_common():
        lines.append(f"  - {cat}: {n}")
    if paper_freq:
        lines.append("")
        top = paper_freq.most_common(3)
        if len(top) == 1 and top[0][1] == 1:
            # Single paper, single run — don't bother with a table.
            pass
        else:
            lines.append("Most-touched papers:")
            for k, n in top:
                lines.append(f"  - {k}: {n} run(s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Section: KB ↔ Zotero orphans (current state, not historical)
# ---------------------------------------------------------------------

def section_orphans(
    kb_root: Path,
    since: datetime, until: datetime,
    **_opts,
) -> str:
    """Report md files / attachment dirs that have no Zotero
    counterpart. Unlike the other sections this reads LIVE state
    (reaches out to Zotero), not events.jsonl — "is this md
    orphan?" is a question about NOW, not about a time window.

    Degrades gracefully:
      - kb_importer not installed → section says so + skips
      - Zotero unreachable (network down, no API key, wrong mode)
        → section reports what can be derived locally, notes the
        Zotero-dependent checks were skipped
    """
    header = (
        f"## Orphans  (live scan, run at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})"
    )

    try:
        from kb_importer.config import load_config
        from kb_importer.commands.orphans_cmd import detect_orphans
    except ImportError as e:
        return (
            f"{header}\n\n"
            f"_(kb_importer not installed; orphan scan skipped: {e})_"
        )

    try:
        cfg = load_config(kb_root=kb_root)
    except Exception as e:
        return (
            f"{header}\n\n"
            f"_(could not load kb-importer config to connect to Zotero: {e})_"
        )

    try:
        result = detect_orphans(cfg)
    except Exception as e:
        return (
            f"{header}\n\n"
            f"_(Zotero unreachable in mode {cfg.zotero_source_mode}; "
            f"orphan scan skipped: {type(e).__name__}: {e})_\n\n"
            "Attachment-level orphan detection requires Zotero; no "
            "partial result produced."
        )

    orphan_papers         = result["orphan_papers"]
    orphan_notes          = result["orphan_notes"]
    unreferenced_archived = result["unreferenced_archived"]

    if not any((orphan_papers, orphan_notes, unreferenced_archived)):
        return f"{header}\n\nNo orphans found."

    lines = [header, ""]
    if orphan_papers:
        lines.append(
            f"Orphan paper mds ({len(orphan_papers)}) — in KB but "
            "deleted from Zotero:"
        )
        # Cap long lists at 10 in the report (full list still via
        # `kb-importer check-orphans`).
        for k in orphan_papers[:10]:
            lines.append(f"  - papers/{k}.md")
        if len(orphan_papers) > 10:
            lines.append(f"  - +{len(orphan_papers) - 10} more "
                         "(run `kb-importer check-orphans` for full list)")
        lines.append("")

    if orphan_notes:
        lines.append(
            f"Orphan note mds ({len(orphan_notes)}) — in KB but "
            "deleted from Zotero:"
        )
        for k in orphan_notes[:10]:
            lines.append(f"  - topics/standalone-note/{k}.md")
        if len(orphan_notes) > 10:
            lines.append(f"  - +{len(orphan_notes) - 10} more")
        lines.append("")

    if unreferenced_archived:
        lines.append(
            f"Archived attachment dirs not referenced by any imported "
            f"md ({len(unreferenced_archived)}):"
        )
        for k in unreferenced_archived[:10]:
            lines.append(f"  - storage/_archived/{k}/")
        if len(unreferenced_archived) > 10:
            lines.append(f"  - +{len(unreferenced_archived) - 10} more")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------
# Section: big library-level operations (import / citations / index)
# ---------------------------------------------------------------------

def section_ops(
    kb_root: Path,
    since: datetime, until: datetime,
    **_opts,
) -> str:
    """Aggregate the three "library-level operation" event types —
    import_run, citations_run, index_op — into one summary block.

    Rationale: these events are coarse (one per command invocation,
    not per paper), and the user usually wants to know "this month I
    ran: 4 imports, 2 citation fetches, 1 reindex" in one glance,
    rather than peeking at three separate sections.

    Breakdown within the block preserves which subcommand ran (e.g.
    "citations_run: fetch=2, link=1") since that context matters.
    """
    from kb_importer.events import (
        EVENT_IMPORT_RUN, EVENT_CITATIONS_RUN, EVENT_INDEX_OP,
    )

    events = _read_events(
        kb_root, since=since, until=until,
        event_types=[EVENT_IMPORT_RUN, EVENT_CITATIONS_RUN, EVENT_INDEX_OP],
    )
    header = _section_header("Library operations", since, until)
    if not events:
        return header + "\n\nNo library-level operations in window."

    # Split by event_type for the per-group breakdown.
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_type[e.get("event_type", "?")].append(e)

    lines = [header, ""]
    lines.append(f"Total: {len(events)} operation(s).")
    lines.append("")

    # import_run group
    imports = by_type.get(EVENT_IMPORT_RUN, [])
    if imports:
        cat_counter = Counter(e.get("category") or "?" for e in imports)
        lines.append(
            f"- `kb-importer import`: {len(imports)} run(s) "
            f"({_cat_summary(cat_counter)})"
        )

    # citations_run group — include subcommand breakdown
    citations = by_type.get(EVENT_CITATIONS_RUN, [])
    if citations:
        cat_counter = Counter(e.get("category") or "?" for e in citations)
        sub_counter: Counter = Counter(
            (e.get("extra") or {}).get("subcommand") or "?"
            for e in citations
        )
        sub_str = ", ".join(f"{s}={n}" for s, n in sub_counter.most_common())
        lines.append(
            f"- `kb-citations`: {len(citations)} run(s) "
            f"({_cat_summary(cat_counter)}; by subcommand: {sub_str})"
        )

    # index_op group — include subcommand breakdown
    index_ops = by_type.get(EVENT_INDEX_OP, [])
    if index_ops:
        cat_counter = Counter(e.get("category") or "?" for e in index_ops)
        sub_counter = Counter(
            (e.get("extra") or {}).get("subcommand") or "?"
            for e in index_ops
        )
        sub_str = ", ".join(f"{s}={n}" for s, n in sub_counter.most_common())
        lines.append(
            f"- `kb-mcp reindex/snapshot`: {len(index_ops)} run(s) "
            f"({_cat_summary(cat_counter)}; by subcommand: {sub_str})"
        )

    # Most recent landmark — handy "when did I last X?" signal.
    latest = events[-1]
    lines.append("")
    lines.append(
        f"Last: {latest.get('ts')} — "
        f"{latest.get('event_type')} "
        f"({(latest.get('extra') or {}).get('subcommand') or latest.get('category')})"
    )

    return "\n".join(lines)


def _cat_summary(c: Counter) -> str:
    """Render '{ok=5, partial=1}' from a Counter of categories."""
    return ", ".join(f"{k}={v}" for k, v in c.most_common())


# ---------------------------------------------------------------------
# Section registry — add new sections here.
# ---------------------------------------------------------------------

SECTION_REGISTRY: dict[str, Callable] = {
    "skip":         section_skip,
    "re_read":      section_re_read,
    "re_summarize": section_re_summarize,
    "ops":          section_ops,
    "orphans":      section_orphans,
}

# Default sections to render when --sections is not passed.
# Order reflects "what's most useful at a glance":
#   - ops first: "what did I do with my library this month"
#   - skip / re_read / re_summarize: what failed / what got touched
#   - orphans last: live scan, slowest, least-frequent payoff
DEFAULT_SECTIONS = ("ops", "skip", "re_read", "re_summarize", "orphans")


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def generate_report(
    kb_root: Path,
    *,
    days: int = 30,
    since: datetime | None = None,
    until: datetime | None = None,
    sections: list[str] | None = None,
    include_normal: bool = False,
) -> str:
    """Produce the full report as a markdown string."""
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")
    end = until or datetime.now(timezone.utc)
    if since is None:
        start = end - timedelta(days=days)
    else:
        start = since
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    if start >= end:
        raise ValueError(
            f"report window start {start.isoformat()} is not before "
            f"end {end.isoformat()}; refusing to generate an empty "
            f"or reversed window."
        )

    chosen = list(sections) if sections else list(DEFAULT_SECTIONS)

    out: list[str] = []
    out.append(
        f"# kb-mcp report — {start.strftime('%Y-%m-%d')} to "
        f"{end.strftime('%Y-%m-%d')}"
    )
    out.append("")
    out.append(f"Window: {start.isoformat()} → {end.isoformat()}")
    out.append("")

    for name in chosen:
        fn = SECTION_REGISTRY.get(name)
        if fn is None:
            out.append(f"(unknown section {name!r} — skipped)")
            out.append("")
            continue
        out.append(fn(
            kb_root, start, end,
            include_normal=include_normal,
        ))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _section_header(title: str, since: datetime, until: datetime,
                    extra_note: str | None = None) -> str:
    days = (until - since).days
    base = (
        f"## {title}  "
        f"({since.strftime('%Y-%m-%d')} → "
        f"{until.strftime('%Y-%m-%d')}, {days} day(s))"
    )
    if extra_note:
        base += f"\n_{extra_note}_"
    return base
