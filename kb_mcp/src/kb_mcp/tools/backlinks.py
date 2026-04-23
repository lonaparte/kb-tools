"""backlinks: "who references me?" — the other half of the link graph.

Given any md path (or bare node key/slug), list all nodes that have
an outgoing edge pointing here. Use to discover unexpected uses:
the paper you're reading is cited in which topic notes? The topic
you're writing was discussed in which thoughts?

Dangling → not ignored. If you used to be dangling and now exist,
running `kb-mcp index` resolves those edges. But an edge pointing
to a deleted node (which we marked dangling in _remove_orphans) will
still list as incoming if you re-create the node — useful
breadcrumb.
"""
from __future__ import annotations

from ..store import Store

# v26 map: various input path forms → node_type.
# Two-segment prefixes (topics/standalone-note/, topics/agent-created/)
# are checked before single-segment ones in _parse_target below.
_PATH_TO_TYPE_SINGLE = {
    "papers": "paper",
    "thoughts": "thought",
}
_PATH_TO_TYPE_DOUBLE = {
    "topics/standalone-note": "note",
    "topics/agent-created": "topic",
}


def backlinks_impl(store: Store, target: str) -> str:
    """Return all edges pointing at `target`.

    Args:
        target: Either a KB-relative md path like "papers/ABCD1234.md"
            or "topics/gfm-stability.md", or a bare node key/slug
            (in which case we try all node types). Path form is
            preferred because it's unambiguous.
    """
    node_type, node_key = _parse_target(target)

    # If we couldn't disambiguate (bare key), search all types.
    if node_type is None:
        rows = store.execute("""
            SELECT src_type, src_key, origin
            FROM links
            WHERE dst_key = ?
            ORDER BY src_type, src_key
        """, (node_key,)).fetchall()
    else:
        rows = store.execute("""
            SELECT src_type, src_key, origin
            FROM links
            WHERE dst_type = ? AND dst_key = ?
            ORDER BY src_type, src_key
        """, (node_type, node_key)).fetchall()

    if not rows:
        label = f"{node_type}/{node_key}" if node_type else node_key
        return f"No backlinks to {label}."

    # Group by src_type for readability.
    groups: dict[str, list[tuple[str, set[str]]]] = {}
    # key → set of origins (same edge via multiple origins collapses).
    merged: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        k = (r["src_type"], r["src_key"])
        merged.setdefault(k, set()).add(r["origin"])
    for (st, sk), origins in merged.items():
        groups.setdefault(st, []).append((sk, origins))

    # Fetch display titles in one batch per type.
    titles = _fetch_titles(store, merged.keys())

    label = f"{node_type}/{node_key}" if node_type else node_key
    lines = [f"Backlinks to {label} ({len(merged)} incoming edge(s)):", ""]
    for st in ("paper", "note", "topic", "thought"):
        items = groups.get(st)
        if not items:
            continue
        lines.append(f"  {_pluralize(st)}:")
        for src_key, origins in sorted(items):
            title = titles.get((st, src_key), "")
            title_part = f"  {title}" if title else ""
            origin_str = "+".join(sorted(origins))
            lines.append(f"    {st}/{src_key}{title_part}  [{origin_str}]")
        lines.append("")
    return "\n".join(lines).rstrip()


def _parse_target(target: str) -> tuple[str | None, str]:
    """Convert various forms to (node_type, key).

    Returns (None, raw_key) when the input is a bare key with no
    subdir hint — caller will then search across all types.

    v26: recognises two-segment prefixes topics/standalone-note/ and
    topics/agent-created/ before falling back to single-segment.
    """
    t = target.strip().strip("/")
    if t.endswith(".md"):
        t = t[:-3]
    if "/" not in t:
        return (None, t)

    # Try two-segment match first (longest-match).
    for prefix, node_type in _PATH_TO_TYPE_DOUBLE.items():
        if t.startswith(prefix + "/"):
            return (node_type, t[len(prefix) + 1:])

    head, _, tail = t.partition("/")
    if head in _PATH_TO_TYPE_SINGLE:
        return (_PATH_TO_TYPE_SINGLE[head], tail)
    return (None, t)


def _fetch_titles(
    store: Store, keys: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Bulk-fetch titles for a set of (node_type, key) pairs."""
    out: dict[tuple[str, str], str] = {}
    by_type: dict[str, list[str]] = {}
    for nt, k in keys:
        by_type.setdefault(nt, []).append(k)
    for nt, ks in by_type.items():
        if nt == "paper":
            # v26: papers PK is now paper_key (md stem).
            table, col = "papers", "paper_key"
        elif nt == "note":
            table, col = "notes", "zotero_key"
        elif nt == "topic":
            table, col = "topics", "slug"
        elif nt == "thought":
            table, col = "thoughts", "slug"
        else:
            continue
        placeholders = ",".join("?" * len(ks))
        for r in store.execute(
            f"SELECT {col} AS pk, title FROM {table} "
            f"WHERE {col} IN ({placeholders})",
            tuple(ks),
        ).fetchall():
            out[(nt, r["pk"])] = r["title"] or ""
    return out


def _pluralize(node_type: str) -> str:
    return {"paper": "Papers", "note": "Notes",
            "topic": "Topics", "thought": "Thoughts"}.get(node_type, node_type)
