"""trace_links: BFS from a starting node through the link graph.

Use when you want to explore: "show me everything connected to
topics/gfm-stability within 2 hops". Direction can be out / in /
both, letting you follow forward refs, backward refs, or the
undirected neighborhood.

Output is a depth-indented tree, cycles pruned by visited set. Max
depth capped at 4 to keep output readable and database queries
bounded (worst case: branching factor ^ depth edges visited).
"""
from __future__ import annotations

from collections import deque

from ..store import Store

# v26 map: two-segment first, then single.
_PATH_TO_TYPE_SINGLE = {
    "papers": "paper",
    "thoughts": "thought",
}
_PATH_TO_TYPE_DOUBLE = {
    "topics/standalone-note": "note",
    "topics/agent-created": "topic",
}

MAX_DEPTH = 4


def trace_links_impl(
    store: Store,
    start: str,
    depth: int = 2,
    direction: str = "out",
) -> str:
    """BFS from `start` to `depth` hops, return indented tree.

    Args:
        start: path form "papers/ABCD1234" or "papers/ABCD1234.md",
               or a "paper/ABCD1234" type-form, or bare key (risky —
               may match multiple node types if keys collide).
        depth: hops. Capped at 4.
        direction: "out" | "in" | "both".
    """
    depth = max(1, min(depth, MAX_DEPTH))
    if direction not in ("out", "in", "both"):
        return f"[error] direction must be out|in|both, got {direction!r}."

    start_type, start_key = _parse_start(start)
    if start_type is None:
        start_type = _guess_type(store, start_key)
        if start_type is None:
            return (
                f"[not found] No paper/note/topic/thought with "
                f"key/slug {start_key!r}. "
                f"If you know the type, prefix it: "
                f"'papers/{start_key}' or 'topics/{start_key}'."
            )

    # BFS.
    visited: set[tuple[str, str]] = {(start_type, start_key)}
    # queue items: (node_type, node_key, depth_remaining, path_list)
    queue: deque = deque([(start_type, start_key, depth, [])])

    # Accumulate lines per depth for structured output.
    out_lines = [f"Trace from {start_type}/{start_key}, depth={depth}, direction={direction}:", ""]
    title0 = _fetch_title(store, start_type, start_key)
    out_lines.append(f"[{start_type}/{start_key}]  {title0}")

    # Collect (depth_from_start, edge) pairs for rendering.
    discovered: list[tuple[int, tuple[str, str], str, tuple[str, str]]] = []
    # Each entry: (depth_of_dst, src, origin, dst)
    while queue:
        nt, nk, rem, path = queue.popleft()
        if rem <= 0:
            continue
        neighbors = _neighbors(store, nt, nk, direction)
        for dst_type, dst_key, origin in neighbors:
            edge_depth = depth - rem + 1
            discovered.append((edge_depth, (nt, nk), origin, (dst_type, dst_key)))
            if (dst_type, dst_key) in visited:
                continue
            # Don't expand dangling edges — nothing to trace from.
            if dst_type == "dangling":
                visited.add((dst_type, dst_key))
                continue
            visited.add((dst_type, dst_key))
            queue.append((dst_type, dst_key, rem - 1, path + [(nt, nk)]))

    if not discovered:
        out_lines.append("  (no outgoing/incoming edges)")
        return "\n".join(out_lines)

    # Fetch titles in bulk.
    all_nodes: set[tuple[str, str]] = set()
    for _, src, _, dst in discovered:
        all_nodes.add(src)
        all_nodes.add(dst)
    titles = _fetch_titles(store, all_nodes)

    # Render as indented list grouped by discovery depth.
    for d in range(1, depth + 1):
        at_depth = [x for x in discovered if x[0] == d]
        if not at_depth:
            break
        out_lines.append("")
        out_lines.append(f"  depth {d}:")
        # Dedupe identical edges.
        seen: set = set()
        for _, (st, sk), origin, (dt, dk) in at_depth:
            key = (st, sk, dt, dk, origin)
            if key in seen:
                continue
            seen.add(key)
            arrow = "→" if direction != "in" else "←"
            src_title = _truncate(titles.get((st, sk), ""), 40)
            dst_title = _truncate(titles.get((dt, dk), ""), 40)
            dst_label = (
                f"{dt}/{dk}" if dt != "dangling" else f"dangling/{dk}"
            )
            indent = "    " * d
            out_lines.append(
                f"{indent}{st}/{sk} {arrow} {dst_label}  "
                f"[{origin}]"
            )
            if dst_title:
                out_lines.append(f"{indent}    {dst_title}")

    return "\n".join(out_lines)


def _neighbors(
    store: Store, node_type: str, node_key: str, direction: str,
) -> list[tuple[str, str, str]]:
    """Return adjacent (dst_type, dst_key, origin) given direction.

    For direction='in', dst_* actually contains the SOURCE node
    (the "other end" of the edge), which is what the trace wants.
    """
    results: list[tuple[str, str, str]] = []
    if direction in ("out", "both"):
        for r in store.execute(
            "SELECT dst_type, dst_key, origin FROM links "
            "WHERE src_type = ? AND src_key = ?",
            (node_type, node_key),
        ).fetchall():
            results.append((r["dst_type"], r["dst_key"], r["origin"]))
    if direction in ("in", "both"):
        for r in store.execute(
            "SELECT src_type, src_key, origin FROM links "
            "WHERE dst_type = ? AND dst_key = ?",
            (node_type, node_key),
        ).fetchall():
            # Mark 'in' edges with trailing " (back)" on origin for clarity.
            results.append((r["src_type"], r["src_key"], r["origin"] + "←"))
    return results


def _parse_start(s: str) -> tuple[str | None, str]:
    """Parse v26 forms: 'papers/ABCD1234[.md]', 'topics/agent-created/X',
    'topics/standalone-note/Y', singular 'paper/KEY', or bare 'KEY'."""
    s = s.strip().strip("/")
    if s.endswith(".md"):
        s = s[:-3]
    if "/" not in s:
        return (None, s)

    # Two-segment match first (longest-match).
    for prefix, node_type in _PATH_TO_TYPE_DOUBLE.items():
        if s.startswith(prefix + "/"):
            return (node_type, s[len(prefix) + 1:])

    head, _, tail = s.partition("/")
    if head in _PATH_TO_TYPE_SINGLE:
        return (_PATH_TO_TYPE_SINGLE[head], tail)
    singular = {"paper", "note", "topic", "thought"}
    if head in singular:
        return (head, tail)
    return (None, s)


def _guess_type(store: Store, key: str) -> str | None:
    """Find which node table holds a given key. Returns None if not
    in any table. Ties broken paper → topic → thought → note.

    v26: papers table PK is paper_key (md stem)."""
    for node_type, table, col in [
        ("paper", "papers", "paper_key"),
        ("topic", "topics", "slug"),
        ("thought", "thoughts", "slug"),
        ("note", "notes", "zotero_key"),
    ]:
        row = store.execute(
            f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (key,)
        ).fetchone()
        if row:
            return node_type
    return None


def _fetch_title(store: Store, node_type: str, key: str) -> str:
    titles = _fetch_titles(store, {(node_type, key)})
    return titles.get((node_type, key), "")


def _fetch_titles(
    store: Store, keys: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    by_type: dict[str, list[str]] = {}
    for nt, k in keys:
        if nt == "dangling":
            continue
        by_type.setdefault(nt, []).append(k)
    for nt, ks in by_type.items():
        if nt == "paper":
            table, col = "papers", "paper_key"  # v26
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


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"
