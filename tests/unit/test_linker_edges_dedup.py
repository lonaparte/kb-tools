"""Regression for the v0.27.10 edges_emitted over-count.

Pre-0.27.10 `build_edges()` did `report.edges_emitted += 1` on
each append. But `references` and `citations` lists from the
provider can produce the same (src, dst, origin) tuple via
different paths (A→X listed in A's references AND also in X's
citations). Downstream `INSERT OR IGNORE` silently collapses
duplicates, so the user-visible counter was larger than the
real row count in the `links` table.

v0.27.10 dedupes at build time so `report.edges_emitted` equals
the number of rows that will actually land in the DB."""
from __future__ import annotations

import json

from conftest import skip_if_no_frontmatter


class _FakeResolver:
    """Stub resolver for linker tests. Returns a fixed mapping."""
    def __init__(self, doi_to_key: dict[str, str]):
        self._map = doi_to_key

    def resolve(self, *, doi=None, title=None):
        if doi and doi in self._map:
            return self._map[doi]
        return None


def _seed_cache(kb_root, src_key: str, data: dict):
    cache_dir = kb_root / ".kb-mcp" / "citations" / "by-paper"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{src_key}.json").write_text(json.dumps(data))


def test_edge_seen_via_both_refs_and_citations_counted_once(tmp_path):
    """Paper A cites B (via A's references list). Paper B also
    lists A as "papers citing B" (via B's citations list). Both
    entries yield the same edge A→B. edges_emitted must count
    it once."""
    skip_if_no_frontmatter()
    from kb_citations.linker import build_edges

    # Two cache files, both referencing the other via DOI.
    _seed_cache(tmp_path, "A", {
        "provider": "test",
        "references": [{"doi": "10.0/B", "title": "paper B"}],
        "citations": [],
    })
    _seed_cache(tmp_path, "B", {
        "provider": "test",
        "references": [],
        "citations": [{"doi": "10.0/A", "title": "paper A"}],
    })

    resolver = _FakeResolver({"10.0/A": "A", "10.0/B": "B"})
    edges, report = build_edges(tmp_path, resolver=resolver)

    # Exactly one unique A→B edge.
    ab_edges = [
        e for e in edges
        if e["src_key"] == "A" and e["dst_key"] == "B"
    ]
    assert len(ab_edges) == 1, (
        f"duplicate edge A→B not collapsed: {ab_edges}"
    )

    assert report.edges_emitted == len(edges), (
        f"edges_emitted ({report.edges_emitted}) != "
        f"len(edges) ({len(edges)}) — counter is still "
        f"counting appends rather than unique rows"
    )


def test_distinct_edges_all_counted(tmp_path):
    """Sanity: disjoint edges are all counted."""
    skip_if_no_frontmatter()
    from kb_citations.linker import build_edges

    _seed_cache(tmp_path, "A", {
        "provider": "test",
        "references": [
            {"doi": "10.0/B", "title": "B"},
            {"doi": "10.0/C", "title": "C"},
        ],
        "citations": [],
    })
    _seed_cache(tmp_path, "B", {
        "provider": "test",
        "references": [{"doi": "10.0/C", "title": "C"}],
        "citations": [],
    })

    resolver = _FakeResolver({
        "10.0/A": "A", "10.0/B": "B", "10.0/C": "C",
    })
    edges, report = build_edges(tmp_path, resolver=resolver)

    # Expected: A→B, A→C, B→C.
    pairs = {(e["src_key"], e["dst_key"]) for e in edges}
    assert pairs == {("A", "B"), ("A", "C"), ("B", "C")}
    assert report.edges_emitted == 3


def test_empty_cache_emits_zero(tmp_path):
    skip_if_no_frontmatter()
    from kb_citations.linker import build_edges

    (tmp_path / ".kb-mcp" / "citations" / "by-paper").mkdir(parents=True)
    resolver = _FakeResolver({})
    edges, report = build_edges(tmp_path, resolver=resolver)
    assert edges == []
    assert report.edges_emitted == 0


def test_unresolved_refs_count_dangling_not_emitted(tmp_path):
    """Refs whose DOI can't be resolved to a local paper go into
    edges_to_dangling, NOT edges_emitted. Regression: the 0.27.10
    dedup rewrite must preserve this split."""
    skip_if_no_frontmatter()
    from kb_citations.linker import build_edges

    _seed_cache(tmp_path, "A", {
        "provider": "test",
        "references": [
            {"doi": "10.0/B", "title": "B (resolvable)"},
            {"doi": "10.0/UNKNOWN", "title": "not in KB"},
        ],
        "citations": [],
    })
    resolver = _FakeResolver({"10.0/A": "A", "10.0/B": "B"})
    edges, report = build_edges(tmp_path, resolver=resolver)

    assert report.edges_emitted == 1  # only A→B
    assert report.edges_to_dangling == 1
