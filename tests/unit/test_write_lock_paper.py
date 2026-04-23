"""Regression for the v0.28.0 per-paper write lock.

v0.28.0 adds `write_lock_paper(kb_root, paper_key)` for
RMW ops that touch a single md (tag add/remove, kb_ref
add/remove, ai-zone append). Multi-paper concurrent writers
can now proceed without serialising on the kb-root-level
`write.lock`; same-paper writers still serialise (so
read-modify-write on `kb_tags` etc. remains race-free).

The v0.27.4 field-report bug "30+ concurrent tag writes
lose updates" was diagnosed during the 0.28.0 review to
actually be a race in the kb-root-level write_lock itself
(O_EXCL + PID-file empty-window). The fcntl.flock rewrite
(see test_write_lock_reentry) fixed that underlying issue.
Per-paper locks are an independent throughput
improvement: they let different papers' writers run
parallel."""
from __future__ import annotations

import subprocess
import sys
import textwrap


def _peer_paper_timeout_script(kb_root, paper_key):
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, "/home/llm-agent/workspace/KB/kb-tools/kb_core/src")
        sys.path.insert(0, "/home/llm-agent/workspace/KB/kb-tools/kb_write/src")
        from pathlib import Path
        from kb_write.atomic import write_lock_paper
        try:
            with write_lock_paper(Path(r"{kb_root}"), "{paper_key}", timeout=0.5):
                sys.exit(0)
        except TimeoutError:
            sys.exit(1)
    """)


def _sibling_can_acquire_paper(kb_root, paper_key):
    r = subprocess.run(
        [sys.executable, "-c",
         _peer_paper_timeout_script(kb_root, paper_key)],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def test_same_paper_serialised(tmp_path):
    """Holding write_lock_paper(kb, P1) blocks another process's
    write_lock_paper(kb, P1)."""
    from kb_write.atomic import write_lock_paper

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock_paper(kb, "P1"):
        assert not _sibling_can_acquire_paper(kb, "P1"), (
            "same-paper lock didn't serialise"
        )

    # Released — peer can now acquire.
    assert _sibling_can_acquire_paper(kb, "P1")


def test_different_papers_parallel(tmp_path):
    """Per-paper locks let different-paper writers proceed
    concurrently. Holding P1's lock must NOT block P2's."""
    from kb_write.atomic import write_lock_paper

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock_paper(kb, "P1"):
        # P1 contended (by us), P2 free.
        assert not _sibling_can_acquire_paper(kb, "P1")
        assert _sibling_can_acquire_paper(kb, "P2"), (
            "per-paper lock was actually kb-root-level — "
            "different paper is blocked even though it shouldn't be"
        )


def test_nested_same_paper_lock(tmp_path):
    """Re-entrant within one process for same paper."""
    from kb_write.atomic import write_lock_paper

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    with write_lock_paper(kb, "P1"):
        with write_lock_paper(kb, "P1"):
            assert not _sibling_can_acquire_paper(kb, "P1")
        # Inner exited; outer still held.
        assert not _sibling_can_acquire_paper(kb, "P1")
    assert _sibling_can_acquire_paper(kb, "P1")


def test_unsafe_paper_key_chars_sanitised(tmp_path):
    """paper_key goes into a file path; sanitise any weird chars
    so a malicious key can't break out of the locks dir. kb-mcp
    already validates md stems upstream but defense-in-depth."""
    from kb_write.atomic import write_lock_paper

    kb = tmp_path
    (kb / ".kb-mcp").mkdir()

    # `../` in key should not traverse.
    with write_lock_paper(kb, "../../evil"):
        # File should exist inside paper-locks/, with sanitised name.
        locks_dir = kb / ".kb-mcp" / "paper-locks"
        assert locks_dir.is_dir()
        # Nothing should have been created outside this dir.
        assert not (kb / "evil.lock").exists()
        assert not (tmp_path / "evil.lock").exists()


def test_30_way_same_paper_tag_add_no_loss(tmp_path):
    """End-to-end: 30 concurrent tag adds to the same md, each
    with a distinct tag value. All 30 must land.

    This is the kb-write tag add pathway, which at 0.27.10 would
    lose tags at 100-way with the old kb-root lock (95/100
    observed in the field). With per-paper lock + fcntl.flock
    at 0.28.0: all 30 land."""
    from conftest import skip_if_no_frontmatter
    skip_if_no_frontmatter()
    import concurrent.futures as cf
    import yaml

    # Build a minimal paper md.
    papers = tmp_path / "papers"
    papers.mkdir()
    md = papers / "P1.md"
    md.write_text(
        "---\nkind: paper\ntitle: t\nkb_tags: []\n---\nbody\n"
    )

    from kb_write.config import WriteContext
    from kb_write.ops import tag as tag_ops

    def one(i):
        ctx = WriteContext(
            kb_root=tmp_path, git_commit=False, reindex=False,
            lock=True, dry_run=False,
        )
        tag_ops.add(ctx, "papers/P1", f"t-{i:02d}")

    N = 30
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        list(ex.map(one, range(N)))

    fm = yaml.safe_load(md.read_text().split("\n---\n")[0][4:])
    final_tags = set(fm.get("kb_tags") or [])
    expected = {f"t-{i:02d}" for i in range(N)}
    missing = expected - final_tags
    assert not missing, (
        f"{len(missing)} tags lost out of {N} "
        f"in a 30-way concurrent add — lock is leaking "
        f"updates. Missing: {sorted(missing)[:10]}"
    )
