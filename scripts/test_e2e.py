"""End-to-end check for ee-kb-tools.

Originated as a v26 migration guard; kept as the canonical regression
sweep. Covers the invariants that multiple recent releases have
touched:

  - schema v6 (papers PK = paper_key)
  - book chapter round-trip (parent + chapter mds, shared zotero_key)
  - list_paper_parts MCP tool
  - find_paper_by_key returns ONLY whole-work
  - path rejection (legacy zotero-notes/ and top-level topics/)
  - index-status deprecated path detection
  - ai_zone append (newest-at-top ordering)
  - events JSONL (6 event types) round-trip + filtering
  - kb-mcp report aggregation (skip + re_read + re_summarize +
    ops + orphans sections)
  - kb-write re-read selectors (7 strategies + registry) + dry-run
    + robustness (empty pool, count=0, over-count, bad args)
  - MCP tool count = 36

Not exhaustive — focuses on what recent releases changed so
regressions surface.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path


PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "kb_core" / "src"))
sys.path.insert(0, str(PKG_ROOT / "kb_write" / "src"))
sys.path.insert(0, str(PKG_ROOT / "kb_mcp" / "src"))
sys.path.insert(0, str(PKG_ROOT / "kb_importer" / "src"))


def ok(msg): print(f"✓ {msg}")
def skip(msg): print(f"- {msg}")


def test_schema_v6():
    from kb_mcp.store import EXPECTED_SCHEMA_VERSION
    assert EXPECTED_SCHEMA_VERSION == 6, EXPECTED_SCHEMA_VERSION
    ok(f"schema EXPECTED_SCHEMA_VERSION = 6")


def test_paths_reject_v25():
    from kb_write.paths import parse_target, PathError
    # Old v25 paths must raise with v26 upgrade hint.
    for bad in ("zotero-notes/ABCD1234", "topics/my-topic"):
        try:
            parse_target(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except PathError as e:
            assert "DEPRECATED" in str(e), str(e)
    ok("v26 parse_target rejects zotero-notes/ and topics/<slug>")


def test_paths_accept_v26():
    from kb_write.paths import parse_target
    for good, (ntype, key) in [
        ("papers/ABCD1234",                          ("paper", "ABCD1234")),
        ("papers/BOOKKEY-ch03.md",                   ("paper", "BOOKKEY-ch03")),
        ("topics/standalone-note/NOTE001",           ("note",  "NOTE001")),
        ("topics/agent-created/gfm-stability",       ("topic", "gfm-stability")),
        ("topics/agent-created/stability/overview",  ("topic", "stability/overview")),
        ("thoughts/2026-04-23-idea",                 ("thought", "2026-04-23-idea")),
    ]:
        addr = parse_target(good)
        assert addr.node_type == ntype, (good, addr)
        assert addr.key == key, (good, addr)
    ok("v26 parse_target accepts all 6 shapes")


def test_is_book_chapter():
    from kb_mcp.paths import is_book_chapter_filename
    assert is_book_chapter_filename("BOOKKEY-ch03.md") == ("BOOKKEY", 3)
    assert is_book_chapter_filename("BOOKKEY-ch99.md") == ("BOOKKEY", 99)
    assert is_book_chapter_filename("BOOKKEY.md") is None
    assert is_book_chapter_filename("normal-paper.md") is None
    ok("is_book_chapter_filename classifies correctly")


def test_deprecated_path_scan(tmp_kb: Path):
    """index-status should flag content at v25 legacy paths."""
    from kb_mcp.tools.index_status import _scan_deprecated_v25_paths

    (tmp_kb / "zotero-notes").mkdir(exist_ok=True)
    (tmp_kb / "zotero-notes" / "OLDNOTE1.md").write_text(
        "---\nkind: note\n---\n"
    )
    (tmp_kb / "topics").mkdir(exist_ok=True)
    (tmp_kb / "topics" / "old-top-topic.md").write_text(
        "---\nkind: topic\n---\n"
    )
    (tmp_kb / "thoughts").mkdir(exist_ok=True)
    (tmp_kb / "thoughts" / "2025-01-01-BOOKKEY-ch05-chapter-slug.md").write_text(
        "---\nkind: thought\n---\n"
    )

    deprecated = _scan_deprecated_v25_paths(tmp_kb)
    assert "zotero-notes" in deprecated, deprecated
    assert "top-topics" in deprecated, deprecated
    assert "book-chapter-thoughts" in deprecated, deprecated
    ok("index-status detects all 3 deprecated-path categories")


def test_book_chapter_schema(tmp_kb: Path):
    """Verify the v6 schema accepts (paper_key, zotero_key) shape.
    This is what v26 relies on — multiple rows sharing zotero_key."""
    import sqlite3
    from kb_mcp.store import Store
    db = tmp_kb / ".kb-mcp" / "index.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    store.ensure_schema()

    now_iso = "2026-04-23T00:00:00Z"

    # Insert parent + 2 chapters sharing zotero_key.
    for pk in ("BOOKKEY02", "BOOKKEY02-ch01", "BOOKKEY02-ch02"):
        store.execute("""
            INSERT INTO papers
                (paper_key, zotero_key, title, authors, md_path,
                 md_mtime, last_indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (pk, "BOOKKEY02", f"title-{pk}", "[]",
              f"papers/{pk}.md", 1.0, now_iso))
    store.commit()

    rows = list(store.execute(
        "SELECT paper_key FROM papers WHERE zotero_key = ? "
        "ORDER BY paper_key", ("BOOKKEY02",)
    ).fetchall())
    assert len(rows) == 3, rows
    assert [r["paper_key"] for r in rows] == [
        "BOOKKEY02", "BOOKKEY02-ch01", "BOOKKEY02-ch02"
    ]
    ok("schema v6: 3 papers share zotero_key, distinct paper_key PK")

    # list_paper_parts (filesystem-level, needs mds on disk)
    papers = tmp_kb / "papers"
    papers.mkdir(exist_ok=True)
    for pk in ("BOOKKEY02", "BOOKKEY02-ch01", "BOOKKEY02-ch02"):
        (papers / f"{pk}.md").write_text(f"# {pk}\n")

    from kb_mcp.tools.find import list_paper_parts_impl
    parts = list_paper_parts_impl(tmp_kb, "BOOKKEY02")
    assert "BOOKKEY02.md" in parts
    assert "(whole work)" in parts
    assert "BOOKKEY02-ch01.md" in parts
    assert "BOOKKEY02-ch02.md" in parts
    ok("list_paper_parts: whole-work + 2 chapters listed")


def test_ai_zone_append(tmp_kb: Path):
    """v26 ai-zone append semantics: newest at top, preserves older.

    This test bypasses git_commit and reindex (both off) and uses a
    no-lock write context to avoid fcntl weirdness in this sandbox.
    """
    import importlib, sys
    # Stub out python-frontmatter via a tiny shim so importing
    # kb_write doesn't pull in the heavy dep (ai_zone doesn't
    # actually use frontmatter — it only manipulates markers).
    if "frontmatter" not in sys.modules:
        class _Frontmatter:
            @staticmethod
            def load(path):
                class _Post:
                    def __init__(self): self.metadata = {}; self.content = ""
                return _Post()
        sys.modules["frontmatter"] = _Frontmatter()

    from kb_write.ops.ai_zone import append as append_zone
    from kb_write.config import WriteContext

    papers = tmp_kb / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    md = papers / "TESTPAP1.md"
    md.write_text(
        "---\nkind: paper\ntitle: test\n---\n\n"
        "## Abstract\n\nx\n\n"
        "<!-- kb-ai-zone-start -->\n\n"
        "### 2026-04-01 — first entry\n\noriginal body\n\n"
        "<!-- kb-ai-zone-end -->\n"
    )

    ctx = WriteContext(
        kb_root=tmp_kb,
        dry_run=False, git_commit=False, reindex=False, lock=False,
    )
    append_zone(
        ctx, "papers/TESTPAP1",
        expected_mtime=md.stat().st_mtime,
        title="second entry",
        body="added later",
        entry_date=date(2026, 4, 23),
    )
    after = md.read_text(encoding="utf-8")
    pos_new = after.index("2026-04-23 — second entry")
    pos_old = after.index("2026-04-01 — first entry")
    assert pos_new < pos_old, "newer should come first"
    assert "original body" in after, "older preserved verbatim"
    ok("ai_zone.append: newest-at-top, preserves older entries")


def test_events_basic(tmp_kb: Path):
    """v26.x: events.jsonl round-trip covering all 6 event types."""
    from kb_importer.events import (
        record_event, read_events,
        EVENT_FULLTEXT_SKIP, EVENT_RE_READ, EVENT_RE_SUMMARIZE,
        EVENT_IMPORT_RUN, EVENT_CITATIONS_RUN, EVENT_INDEX_OP,
        REASON_QUOTA_EXHAUSTED, REASON_LLM_BAD_REQUEST,
        RE_READ_SUCCESS, RE_READ_DRYRUN,
        RE_SUMMARIZE_SUCCESS, RE_SUMMARIZE_NO_CHANGE,
        IMPORT_RUN_OK, IMPORT_RUN_PARTIAL,
        CITATIONS_RUN_OK,
        INDEX_OP_OK,
    )
    # fulltext_skip events
    record_event(
        tmp_kb, event_type=EVENT_FULLTEXT_SKIP,
        paper_key="AAAA0001", category=REASON_QUOTA_EXHAUSTED,
        detail="daily", provider="gemini", pipeline="short",
    )
    record_event(
        tmp_kb, event_type=EVENT_FULLTEXT_SKIP,
        paper_key="BBBB0002", category=REASON_LLM_BAD_REQUEST,
        detail="HTTP 400", provider="gemini",
        model_tried="gemini-3.1-pro-preview", pipeline="short",
    )
    # re_read events
    record_event(
        tmp_kb, event_type=EVENT_RE_READ,
        paper_key="CCCC0003", category=RE_READ_SUCCESS,
        detail="3/7 sections updated",
        extra={"selector": "unread-first", "source": "papers"},
    )
    record_event(
        tmp_kb, event_type=EVENT_RE_READ,
        paper_key="DDDD0004", category=RE_READ_DRYRUN,
        detail="dry-run preview",
    )
    # re_summarize events
    record_event(
        tmp_kb, event_type=EVENT_RE_SUMMARIZE,
        paper_key="EEEE0005", category=RE_SUMMARIZE_SUCCESS,
        detail="2 of 7 sections updated",
        extra={"sections_updated": 2},
    )
    record_event(
        tmp_kb, event_type=EVENT_RE_SUMMARIZE,
        paper_key="FFFF0006", category=RE_SUMMARIZE_NO_CHANGE,
        detail="all sections judged correct; no splice",
    )
    # Library-operation events (v26.x)
    record_event(
        tmp_kb, event_type=EVENT_IMPORT_RUN,
        category=IMPORT_RUN_OK,
        detail="target=papers metadata=5/5",
        pipeline="import",
        extra={
            "target": "papers", "metadata_success": 5,
            "metadata_failed": 0, "wants_fulltext": False,
        },
    )
    record_event(
        tmp_kb, event_type=EVENT_IMPORT_RUN,
        category=IMPORT_RUN_PARTIAL,
        detail="target=papers metadata=10/12",
        extra={"target": "papers", "metadata_failed": 2},
    )
    record_event(
        tmp_kb, event_type=EVENT_CITATIONS_RUN,
        category=CITATIONS_RUN_OK,
        detail="fetch: ok=12 err=0",
        pipeline="citations",
        extra={"subcommand": "fetch", "provider": "semantic-scholar"},
    )
    record_event(
        tmp_kb, event_type=EVENT_INDEX_OP,
        category=INDEX_OP_OK,
        detail="reindex: rc=0",
        pipeline="kb_mcp",
        extra={"subcommand": "reindex", "provider": "openai"},
    )

    all_events = read_events(tmp_kb)
    assert len(all_events) == 10, len(all_events)

    # Per-type filtering
    skips = read_events(tmp_kb, event_types=[EVENT_FULLTEXT_SKIP])
    assert len(skips) == 2
    rereads = read_events(tmp_kb, event_types=[EVENT_RE_READ])
    assert len(rereads) == 2
    resummarizes = read_events(tmp_kb, event_types=[EVENT_RE_SUMMARIZE])
    assert len(resummarizes) == 2
    imports = read_events(tmp_kb, event_types=[EVENT_IMPORT_RUN])
    assert len(imports) == 2
    cits = read_events(tmp_kb, event_types=[EVENT_CITATIONS_RUN])
    assert len(cits) == 1
    assert cits[0]["extra"]["subcommand"] == "fetch"
    idx_ops = read_events(tmp_kb, event_types=[EVENT_INDEX_OP])
    assert len(idx_ops) == 1
    assert idx_ops[0]["extra"]["subcommand"] == "reindex"

    # On-disk JSONL validity
    jsonl = tmp_kb / ".kb-mcp" / "events.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 10
    for line in lines:
        json.loads(line)

    ok("events: 10 entries (6 event types) round-trip; per-type filtering works")


def test_selectors_basic(tmp_kb: Path):
    """Smoke-test the core selectors with fake candidates."""
    from kb_write.selectors import (
        PaperInfo, REGISTRY, DEFAULT_SELECTOR_NAME,
    )
    # Fake pool: 6 papers, 3 never-summarized, 3 summarized at
    # different mtimes.
    pool = [
        PaperInfo("A001", "papers/A001.md", 1000.0, fulltext_processed=False),
        PaperInfo("A002", "papers/A002.md", 2000.0, fulltext_processed=False),
        PaperInfo("A003", "papers/A003.md", 3000.0, fulltext_processed=False),
        PaperInfo("B001", "papers/B001.md", 100.0, fulltext_processed=True,
                  kb_tags=("foundational",)),
        PaperInfo("B002", "papers/B002.md", 200.0, fulltext_processed=True),
        PaperInfo("B003", "papers/B003.md", 5000.0, fulltext_processed=True),
    ]

    # 1. random: count-bounded, subset of pool
    chosen = REGISTRY["random"].select(pool, count=3, kb_root=tmp_kb, seed=42)
    assert len(chosen) == 3
    assert set(chosen) <= {p.paper_key for p in pool}

    # 2. unread-first (default): prefer keys with no re_read history.
    #    No events yet → unread = full pool → picks any 3.
    chosen = REGISTRY[DEFAULT_SELECTOR_NAME].select(
        pool, count=3, kb_root=tmp_kb, seed=42,
    )
    assert len(chosen) == 3

    # 3. never-summarized: only returns fulltext_processed=False
    chosen = REGISTRY["never-summarized"].select(
        pool, count=5, kb_root=tmp_kb, seed=42,
    )
    assert set(chosen) <= {"A001", "A002", "A003"}
    assert len(chosen) == 3  # only 3 available

    # 4. stale-first: oldest mtime first → B001 (100), B002 (200), A001 (1000)
    chosen = REGISTRY["stale-first"].select(
        pool, count=3, kb_root=tmp_kb, seed=42,
    )
    assert chosen[0] == "B001"
    assert chosen[1] == "B002"

    # 5. by-tag: requires tag arg
    chosen = REGISTRY["by-tag"].select(
        pool, count=5, kb_root=tmp_kb, seed=42, tag="foundational",
    )
    assert chosen == ["B001"]

    ok("selectors: random, unread-first, never-summarized, stale-first, by-tag all work")


def test_selectors_robustness(tmp_kb: Path):
    """All 7 selectors survive boundary inputs: empty pool,
    count=0, count>pool, no kb-mcp DB for related-to-recent,
    malformed fulltext_extracted_at for oldest-summary-first,
    case-mismatched tag for by-tag.
    """
    from kb_write.selectors import PaperInfo, REGISTRY

    empty_pool: list[PaperInfo] = []
    small_pool = [
        PaperInfo("K001", "papers/K001.md", 1000.0,
                  fulltext_processed=True, kb_tags=("Foundational",)),
        PaperInfo("K002", "papers/K002.md", 2000.0,
                  fulltext_processed=True, kb_tags=("review",)),
    ]

    # Every registered selector must return [] on empty pool.
    for name, sel in REGISTRY.items():
        if name == "by-tag":
            # by-tag requires tag arg — give one so we're really
            # testing the "empty pool" path.
            chosen = sel.select(empty_pool, count=5, kb_root=tmp_kb,
                                seed=1, tag="irrelevant")
        else:
            chosen = sel.select(empty_pool, count=5, kb_root=tmp_kb, seed=1)
        assert chosen == [], f"{name}: empty pool should return []"

    # count=0 on non-empty pool → [] from every selector.
    for name, sel in REGISTRY.items():
        if name == "by-tag":
            chosen = sel.select(small_pool, count=0, kb_root=tmp_kb,
                                seed=1, tag="foundational")
        else:
            chosen = sel.select(small_pool, count=0, kb_root=tmp_kb, seed=1)
        assert chosen == [], f"{name}: count=0 should return []"

    # count > pool → at most len(pool).
    for name, sel in REGISTRY.items():
        if name == "by-tag":
            chosen = sel.select(small_pool, count=99, kb_root=tmp_kb,
                                seed=1, tag="foundational")
        else:
            chosen = sel.select(small_pool, count=99, kb_root=tmp_kb, seed=1)
        assert len(chosen) <= len(small_pool), \
            f"{name}: count > pool should cap at pool size"

    # by-tag is case-insensitive: frontmatter "Foundational" vs CLI
    # "foundational" must match.
    chosen = REGISTRY["by-tag"].select(
        small_pool, count=5, kb_root=tmp_kb, seed=1, tag="foundational",
    )
    assert chosen == ["K001"], chosen

    # by-tag supports `tags=a,b` multi-match.
    chosen = REGISTRY["by-tag"].select(
        small_pool, count=5, kb_root=tmp_kb, seed=1, tags="review,foundational",
    )
    assert set(chosen) == {"K001", "K002"}, chosen

    # related-to-recent with anchor_days=abc → explicit ValueError.
    try:
        REGISTRY["related-to-recent"].select(
            small_pool, count=3, kb_root=tmp_kb, seed=1, anchor_days="abc",
        )
        assert False, "related-to-recent should reject non-int anchor_days"
    except ValueError:
        pass

    # related-to-recent with anchor_days=0 → ValueError (must be positive).
    try:
        REGISTRY["related-to-recent"].select(
            small_pool, count=3, kb_root=tmp_kb, seed=1, anchor_days="0",
        )
        assert False, "related-to-recent should reject anchor_days=0"
    except ValueError:
        pass

    # related-to-recent with no kb-mcp DB present → falls back to
    # the `fallback` selector (unread-first by default). Shouldn't
    # crash; should return at most `count`.
    chosen = REGISTRY["related-to-recent"].select(
        small_pool, count=2, kb_root=tmp_kb, seed=1,
    )
    assert len(chosen) <= 2

    # oldest-summary-first with no fulltext_extracted_at in md
    # (papers/K*.md doesn't exist on disk → can't read extracted_at)
    # → all treated as "very old", seed-deterministic order returns
    # something reasonable without crashing.
    chosen = REGISTRY["oldest-summary-first"].select(
        small_pool, count=2, kb_root=tmp_kb, seed=1,
    )
    assert len(chosen) == 2
    assert set(chosen) <= {"K001", "K002"}

    ok("selectors: all 7 robust to empty/zero/over-count/bad-arg inputs")


def test_selectors_registry():
    """Registry has all 7 selectors. Unread-first is default."""
    from kb_write.selectors import REGISTRY, DEFAULT_SELECTOR_NAME
    expected = {
        "random", "unread-first", "stale-first", "never-summarized",
        "oldest-summary-first", "by-tag", "related-to-recent",
    }
    assert set(REGISTRY.keys()) == expected, set(REGISTRY.keys())
    assert DEFAULT_SELECTOR_NAME == "unread-first"
    ok("selectors registry: 7 selectors registered, unread-first is default")


def test_report_generation(tmp_kb: Path):
    """generate_report produces markdown with ops/skip/re_read/re_summarize
    aggregation; orphans section degrades gracefully when Zotero is
    unreachable."""
    from kb_importer.events import (
        record_event,
        EVENT_FULLTEXT_SKIP, EVENT_RE_READ, EVENT_RE_SUMMARIZE,
        EVENT_IMPORT_RUN, EVENT_CITATIONS_RUN, EVENT_INDEX_OP,
        REASON_QUOTA_EXHAUSTED, REASON_PDF_MISSING, REASON_ALREADY_PROCESSED,
        RE_READ_SUCCESS,
        RE_SUMMARIZE_SUCCESS, RE_SUMMARIZE_NO_CHANGE,
        IMPORT_RUN_OK, IMPORT_RUN_PARTIAL,
        CITATIONS_RUN_OK, INDEX_OP_OK,
    )
    from kb_mcp.tools.report import generate_report

    # 3 quota, 2 pdf, 5 already_processed (noise), 1 re_read_success,
    # 2 re_summarize events, 3 library-ops events
    for k in ("A001", "A002", "A003"):
        record_event(
            tmp_kb, event_type=EVENT_FULLTEXT_SKIP,
            paper_key=k, category=REASON_QUOTA_EXHAUSTED,
        )
    for k in ("B001", "B002"):
        record_event(
            tmp_kb, event_type=EVENT_FULLTEXT_SKIP,
            paper_key=k, category=REASON_PDF_MISSING,
        )
    for k in ("C001",) * 5:
        record_event(
            tmp_kb, event_type=EVENT_FULLTEXT_SKIP,
            paper_key=k, category=REASON_ALREADY_PROCESSED,
        )
    record_event(
        tmp_kb, event_type=EVENT_RE_READ,
        paper_key="X001", category=RE_READ_SUCCESS,
        extra={"selector": "unread-first", "source": "papers"},
    )
    record_event(
        tmp_kb, event_type=EVENT_RE_SUMMARIZE,
        paper_key="Y001", category=RE_SUMMARIZE_SUCCESS,
        extra={"sections_updated": 3},
    )
    record_event(
        tmp_kb, event_type=EVENT_RE_SUMMARIZE,
        paper_key="Y002", category=RE_SUMMARIZE_NO_CHANGE,
    )
    # Library-ops events
    record_event(
        tmp_kb, event_type=EVENT_IMPORT_RUN,
        category=IMPORT_RUN_OK,
        extra={"target": "papers", "metadata_success": 5, "metadata_failed": 0},
    )
    record_event(
        tmp_kb, event_type=EVENT_IMPORT_RUN,
        category=IMPORT_RUN_PARTIAL,
        extra={"target": "papers", "metadata_success": 8, "metadata_failed": 2},
    )
    record_event(
        tmp_kb, event_type=EVENT_CITATIONS_RUN,
        category=CITATIONS_RUN_OK,
        extra={"subcommand": "fetch"},
    )
    record_event(
        tmp_kb, event_type=EVENT_INDEX_OP,
        category=INDEX_OP_OK,
        extra={"subcommand": "reindex"},
    )

    # Explicit sections (avoid orphans section which needs Zotero).
    text = generate_report(
        tmp_kb, days=30,
        sections=["ops", "skip", "re_read", "re_summarize"],
    )
    assert "quota_exhausted: 3" in text, text
    assert "pdf_missing: 2" in text, text
    assert "already_processed" not in text or "excludes" in text
    assert "re-read" in text.lower()
    assert "re-summarize" in text.lower()
    assert "success: 1" in text or "no_change: 1" in text, text
    # ops section
    assert "Library operations" in text, text
    assert "kb-importer import" in text, text
    assert "kb-citations" in text, text
    assert "kb-mcp reindex/snapshot" in text, text
    assert "fetch=1" in text, text        # citations subcommand breakdown
    assert "reindex=1" in text, text      # index_op subcommand breakdown

    # --include-normal keeps already_processed
    text2 = generate_report(
        tmp_kb, days=30, sections=["skip"], include_normal=True,
    )
    assert "already_processed: 5" in text2, text2

    # orphans section: no Zotero available → graceful degrade message
    text3 = generate_report(tmp_kb, days=30, sections=["orphans"])
    assert "## Orphans" in text3, text3
    # Must fail gracefully rather than raise — one of these markers
    # indicates the degrade path fired:
    assert any(marker in text3 for marker in (
        "kb_importer not installed",
        "could not load kb-importer config",
        "Zotero unreachable",
        "No orphans found",  # (if cfg happens to load + scan returns empty)
    )), text3

    ok("report: ops + skip + re_read + re_summarize aggregation; orphans degrades gracefully")


def test_re_read_dryrun(tmp_kb: Path):
    """re-read in dry-run mode: selects papers, writes DRYRUN events,
    doesn't call LLM."""
    from kb_write.config import WriteContext
    from kb_write.ops.re_read import re_read
    from kb_importer.events import read_events, EVENT_RE_READ

    # Create 3 paper mds so source_papers has something to return.
    papers_dir = tmp_kb / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    for k in ("D001", "D002", "D003"):
        (papers_dir / f"{k}.md").write_text(
            "---\n"
            f"paper_key: {k}\n"
            "kind: paper\n"
            "fulltext_processed: false\n"
            "---\n\n"
            "body\n",
            encoding="utf-8",
        )

    ctx = WriteContext(
        kb_root=tmp_kb,
        git_commit=False, reindex=False, lock=False,
        dry_run=False,
        actor="test",
    )
    report = re_read(
        ctx, count=2, source_name="papers",
        selector_name="random", seed=0,
        dry_run=True,
    )
    assert report.count_selected == 2
    assert len(report.chosen_keys) == 2
    assert set(report.chosen_keys) <= {"D001", "D002", "D003"}
    assert report.dry_run is True

    # DRYRUN events were written.
    events = read_events(tmp_kb, event_types=[EVENT_RE_READ])
    assert len(events) == 2
    for e in events:
        assert e["category"] == "dryrun_selected"
        assert e["paper_key"] in {"D001", "D002", "D003"}

    ok("re-read dry-run: selects 2/3, logs dryrun_selected events, no LLM")


def test_mcp_tool_count():
    """v26.x: 35 v26 tools + kb_report = 36."""
    src = (PKG_ROOT / "kb_mcp" / "src" / "kb_mcp" / "server.py").read_text()
    count = src.count("@mcp.tool()")
    assert count == 36, f"expected 36, got {count}"
    ok(f"MCP tool count = {count} (v26.x: +kb_report)")


def test_longform_writes_paper_md(tmp_kb: Path):
    """longform._write_chapter_paper produces papers/<KEY>-chNN.md
    with kind=paper and shared zotero_key."""
    import kb_importer.longform as lf
    from kb_importer.config import load_config

    # Minimal cfg: only needs kb_root and kb_root/.ee-kb-tools-style
    # config. load_config will error without a config file; fake
    # it by constructing Config directly.
    from kb_importer.config import Config
    cfg = Config(kb_root=tmp_kb, zotero_storage_dir=tmp_kb)

    # Build a minimal Chapter object.
    from dataclasses import dataclass
    @dataclass
    class _Ch:
        number: int
        title: str
        pages: str
        start_page: int
        end_page: int

    ch = _Ch(number=7, title="Test chapter", pages="100-120",
             start_page=100, end_page=120)

    (tmp_kb / "papers").mkdir(parents=True, exist_ok=True)
    chapter_key, md_path = lf._write_chapter_paper(
        cfg=cfg, paper_key="MYBOOK99",
        paper_title="My Book", chapter=ch,
        body="## 1. Content\n\nchapter body",
        date_iso="2026-04-23",
    )
    assert chapter_key == "MYBOOK99-ch07", chapter_key
    assert md_path.name == "MYBOOK99-ch07.md"
    text = md_path.read_text(encoding="utf-8")
    assert "kind: paper" in text
    assert "zotero_key: MYBOOK99" in text
    assert "item_type: book_chapter" in text
    assert "kb_refs: [papers/MYBOOK99]" in text
    ok("longform: writes papers/<KEY>-chNN.md with kind=paper")


def test_link_extractor_v26_prefixes():
    """Self-audit regression: link_extractor must honour v26 two-segment
    subdir prefixes (`topics/agent-created/`, `topics/standalone-note/`).
    A bug caught in the v26 self-audit where the old `_SUBDIR_TO_TYPE`
    lookup made all `topics/agent-created/*` kb_refs dangle silently."""
    from kb_mcp.link_extractor import (
        _classify_subdir_prefix, _from_frontmatter,
        _from_wikilinks, _from_mdlinks,
    )
    # classifier: v26 prefixes + legacy fall-through
    cases = [
        ("papers/ABCD1234",                        ("paper", "ABCD1234")),
        ("papers/BOOKKEY-ch03",                    ("paper", "BOOKKEY-ch03")),
        ("topics/standalone-note/NOTE001",         ("note",  "NOTE001")),
        ("topics/agent-created/gfm-stability",     ("topic", "gfm-stability")),
        ("topics/agent-created/stability/overview",("topic", "stability/overview")),
        ("thoughts/2026-04-23-idea",               ("thought","2026-04-23-idea")),
        ("bare-key",                               (None, "bare-key")),
        # legacy forms MUST NOT silently map to a type — they must go
        # to hint_type=None so resolver marks them dangling.
        ("zotero-notes/OLDK1234",                  (None, "zotero-notes/OLDK1234")),
        ("topics/bare-topic",                      (None, "topics/bare-topic")),
    ]
    for inp, expected in cases:
        got = _classify_subdir_prefix(inp)
        assert got == expected, f"{inp!r}: got {got}, want {expected}"

    # mdlink regex must capture two-segment subdirs.
    body = (
        "[a](papers/BOOKKEY-ch03.md) "
        "[b](topics/agent-created/foo.md) "
        "[c](topics/standalone-note/N1.md)"
    )
    refs = list(_from_mdlinks(body))
    pairs = {(r.key, r.hint_type) for r in refs}
    assert ("BOOKKEY-ch03", "paper") in pairs
    assert ("foo", "topic") in pairs
    assert ("N1", "note") in pairs
    ok("link_extractor: v26 two-segment prefixes parsed; legacy → dangling")


def test_sql_joins_use_paper_key():
    """Self-audit regression: every SQL JOIN between papers and another
    table (links, paper_fts, paper_attachments, paper_chunk_meta) must
    use `paper_key`, not `zotero_key`. v25's PK was zotero_key; v26's
    is paper_key. Missing this made reverse_lookup and citation_stats
    silently wrong for book chapters."""
    from pathlib import Path
    src_root = PKG_ROOT / "kb_mcp" / "src" / "kb_mcp"
    offenders = []
    for py in src_root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        text = py.read_text()
        # Any SQL matching "= p.zotero_key" or "p.zotero_key =" in a
        # JOIN / ON context would be the bug. Allow SELECT list uses
        # like "SELECT p.zotero_key, ..." since those are display only.
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            # Flag JOIN / ON clauses that equate zotero_key with another
            # column (the buggy pattern).
            if ("ON " in line or "JOIN " in line) and "zotero_key" in line:
                offenders.append(f"{py.relative_to(PKG_ROOT)}:{i}  {stripped[:100]}")
            # Flag WHERE paper_key filters that use zotero_key column
            # when the semantic is "find by md stem" (another v25 relic).
            if "WHERE" in line and "p.zotero_key =" in line:
                offenders.append(f"{py.relative_to(PKG_ROOT)}:{i}  {stripped[:100]}")
    assert not offenders, (
        "SQL JOINs still using p.zotero_key as FK target:\n  "
        + "\n  ".join(offenders)
    )
    ok("SQL JOINs: no zotero_key used as FK target in kb_mcp")


def test_deadcode_purged():
    """Self-audit regression: ensure removed dead code stays removed.
    If a future change accidentally reintroduces these helpers without
    adding a call site, this test catches it early."""
    from pathlib import Path
    checks = [
        ("kb_write/src/kb_write/paths.py",
         ["_DEPRECATED_V25_DIRS = {"]),
        ("kb_mcp/src/kb_mcp/paths.py",
         ["SUBDIR_FOR_TYPE =", "TYPE_FOR_SUBDIR =",
          "DEPRECATED_SUBDIRS =", "def is_legacy_top_level_topic"]),
        ("kb_importer/src/kb_importer/longform.py",
         ["def _slugify", "_write_chapter_thought = _write_chapter_paper"]),
        ("kb_write/src/kb_write/ops/re_summarize.py",
         ["_log_changelog_entry", ", safe_resolve"]),
    ]
    for rel, forbidden_strings in checks:
        text = (PKG_ROOT / rel).read_text()
        for f in forbidden_strings:
            assert f not in text, (
                f"dead code reintroduced in {rel}: {f!r}"
            )
    ok("dead-code purge: 9 symbols stay removed")


def test_prompt_fragments_no_stale_strings():
    """Agent-facing prompt fragments must not carry v25 strings that
    would actively mislead the agent. Allowed: strings inside
    "deprecated" tables explicitly labelled with "was" / "v25".

    Runs against every md under kb_write/src/kb_write/prompts/fragments/.
    """
    from pathlib import Path
    fragments_dir = PKG_ROOT / "kb_write" / "src" / "kb_write" / "prompts" / "fragments"
    # (term, context-lines-that-make-it-OK)
    stale_terms = {
        # deprecated filename the old skip_log wrote
        "fulltext-skips.jsonl": (),
        # replaced by ai_zone.append
        "update_ai_zone": (),
        # module deleted; events.py replaced it
        "skip_log": (),
    }
    offenders: list[str] = []
    for md in fragments_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        for term, allow_if_contexts in stale_terms.items():
            for lineno, line in enumerate(text.splitlines(), 1):
                if term in line:
                    # If any allow-context substring appears on the
                    # same line, the mention is explanatory — let it
                    # through.
                    if any(c in line for c in allow_if_contexts):
                        continue
                    offenders.append(
                        f"{md.relative_to(PKG_ROOT)}:{lineno}  {term!r}"
                    )
    assert not offenders, (
        "prompt fragments contain stale v25 strings that would "
        "mislead the agent:\n  " + "\n  ".join(offenders)
    )
    ok("prompt fragments: no stale v25 strings (skip_log, fulltext-skips.jsonl, update_ai_zone)")


def main():
    print("=== ee-kb-tools E2E ===")
    test_schema_v6()
    test_paths_reject_v25()
    test_paths_accept_v26()
    test_is_book_chapter()
    test_mcp_tool_count()

    # v26 self-audit regressions (added after the consistency audit
    # found 4 real bugs + 5 dead symbols):
    test_link_extractor_v26_prefixes()
    test_sql_joins_use_paper_key()
    test_deadcode_purged()
    test_prompt_fragments_no_stale_strings()

    # v26.x selector registry (no tmp_kb needed).
    test_selectors_registry()

    with tempfile.TemporaryDirectory() as td1:
        test_book_chapter_schema(Path(td1))
    with tempfile.TemporaryDirectory() as td2:
        test_deprecated_path_scan(Path(td2))
    with tempfile.TemporaryDirectory() as td3:
        test_ai_zone_append(Path(td3))
    with tempfile.TemporaryDirectory() as td4:
        test_events_basic(Path(td4))
    with tempfile.TemporaryDirectory() as td5:
        test_longform_writes_paper_md(Path(td5))
    with tempfile.TemporaryDirectory() as td6:
        test_selectors_basic(Path(td6))
    with tempfile.TemporaryDirectory() as td6b:
        test_selectors_robustness(Path(td6b))
    with tempfile.TemporaryDirectory() as td7:
        test_report_generation(Path(td7))
    with tempfile.TemporaryDirectory() as td8:
        test_re_read_dryrun(Path(td8))
    print("=== E2E all passed ===")


if __name__ == "__main__":
    main()
