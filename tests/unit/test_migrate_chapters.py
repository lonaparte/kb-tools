"""Tests for `kb-write migrate-legacy-chapters` — the one-shot
migration that moves v25-style chapter thoughts to the v26
`papers/<KEY>-chNN.md` canonical location.

Focus:
  - filename + frontmatter detection (positive + negative)
  - idempotent re-run (target exists with same key/chapter → skip)
  - collision detection (target exists with DIFFERENT chapter → report)
  - dry-run doesn't write
  - applied migration produces well-formed v26 chapter md
    (kind=paper, zotero_key, chapter_number, fulltext markers,
    empty ai zone)
  - original body is preserved byte-for-byte
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import skip_if_no_frontmatter


def _legacy_chapter_md(
    date: str = "2026-04-22",
    key: str = "BOOKKEY1",
    chno: int = 3,
    slug: str = "something-interesting",
    *,
    title: str = (
        "Passivity-based Control — Chapter 3: "
        "Average models for DC-DC converters"
    ),
    body: str = (
        "*From [[papers/BOOKKEY1|Passivity-based Control]], chapter 3.*\n\n"
        "## 章节概览\n\n"
        "本章讨论平均模型...\n"
    ),
) -> tuple[str, str]:
    """Return (filename, md_text) for a plausible v25 legacy chapter."""
    filename = f"{date}-{key}-ch{chno:02d}-{slug}.md"
    text = (
        "---\n"
        "kind: thought\n"
        f'title: "{title}"\n'
        f"source_paper: papers/{key}\n"
        f"source_chapter: {chno}\n"
        "source_type: book_chapter\n"
        f"kb_refs: [papers/{key}]\n"
        "kb_tags: [longform]\n"
        "---\n"
        f"{body}"
    )
    return filename, text


def _kb_root(tmp_path):
    (tmp_path / "thoughts").mkdir()
    (tmp_path / "papers").mkdir()
    return tmp_path


def _ctx(kb_root, *, git_commit=False, reindex=False, lock=False):
    from kb_write.config import WriteContext
    return WriteContext(
        kb_root=kb_root,
        git_commit=git_commit, reindex=reindex, lock=lock,
        dry_run=False,
    )


class TestDetection:
    """The filename pattern + frontmatter signals together decide
    whether a file is a legacy chapter."""

    def test_valid_chapter_is_detected(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        fname, text = _legacy_chapter_md()
        (kb / "thoughts" / fname).write_text(text)

        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb), dry_run=True)
        assert len(r.plans) == 1
        plan = r.plans[0]
        assert plan.parent_key == "BOOKKEY1"
        assert plan.chapter_number == 3
        assert plan.dst == kb / "papers" / "BOOKKEY1-ch03.md"
        assert plan.parent_title == "Passivity-based Control"
        assert plan.chapter_title.startswith(
            "Average models for DC-DC converters"
        )

    def test_ordinary_thought_not_detected(self, tmp_path):
        """A thought whose filename happens to contain `-ch03-` must
        not be treated as a legacy chapter if the frontmatter isn't
        a chapter thought (no source_chapter, no source_type)."""
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        (kb / "thoughts" / "2026-04-22-BOOKKEY1-ch03-random.md").write_text(
            "---\nkind: thought\ntitle: Random thought\n---\nbody\n"
        )
        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb), dry_run=True)
        assert len(r.plans) == 0

    def test_non_chapter_filename_skipped(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        (kb / "thoughts" / "2026-04-22-random-idea.md").write_text(
            "---\nkind: thought\ntitle: x\n---\nbody\n"
        )
        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb), dry_run=True)
        assert len(r.plans) == 0


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        fname, text = _legacy_chapter_md()
        (kb / "thoughts" / fname).write_text(text)

        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb), dry_run=True)
        assert r.dry_run is True
        # Source still there.
        assert (kb / "thoughts" / fname).exists()
        # Target never created.
        assert not (kb / "papers" / "BOOKKEY1-ch03.md").exists()
        # Nothing migrated.
        assert len(r.migrated) == 0


class TestIdempotency:
    def test_target_with_same_key_chno_is_already_migrated(self, tmp_path):
        """If papers/<KEY>-chNN.md already exists with matching
        zotero_key + chapter_number, the old thought is skipped."""
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        fname, text = _legacy_chapter_md(key="BOOKKEY2", chno=5)
        (kb / "thoughts" / fname).write_text(text)
        (kb / "papers" / "BOOKKEY2-ch05.md").write_text(
            "---\n"
            "kind: paper\n"
            "zotero_key: BOOKKEY2\n"
            "chapter_number: 5\n"
            "title: x\n"
            "---\nbody\n"
        )

        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb))
        assert len(r.migrated) == 0
        assert len(r.skipped_already_done) == 1
        assert len(r.skipped_collision) == 0
        # Old thought was NOT deleted — it's the user's copy, we
        # don't touch already-migrated state.
        assert (kb / "thoughts" / fname).exists()


class TestCollision:
    def test_target_exists_with_different_chapter_is_collision(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        fname, text = _legacy_chapter_md(key="BOOKKEY3", chno=7)
        (kb / "thoughts" / fname).write_text(text)
        # Squat the target with a different chapter_number.
        (kb / "papers" / "BOOKKEY3-ch07.md").write_text(
            "---\nkind: paper\nzotero_key: OTHERKEY\nchapter_number: 99\n"
            "title: squatted\n---\nbody\n"
        )

        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb))
        assert len(r.migrated) == 0
        assert len(r.skipped_collision) == 1
        plan, reason = r.skipped_collision[0]
        assert "not this chapter" in reason
        assert "zotero_key='OTHERKEY'" in reason or "OTHERKEY" in reason
        # Old thought still in place — user can inspect.
        assert (kb / "thoughts" / fname).exists()


class TestApply:
    def test_new_md_has_v26_canonical_shape(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        fname, text = _legacy_chapter_md(key="BOOKKEY4", chno=11)
        (kb / "thoughts" / fname).write_text(text)

        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb))
        assert len(r.migrated) == 1

        new = (kb / "papers" / "BOOKKEY4-ch11.md")
        assert new.exists(), "new papers/<KEY>-chNN.md not created"
        txt = new.read_text()

        # Frontmatter invariants.
        assert "kind: paper" in txt
        assert "zotero_key: BOOKKEY4" in txt
        assert "item_type: book_chapter" in txt
        assert "chapter_number: 11" in txt
        assert "parent_paper: papers/BOOKKEY4" in txt
        assert "fulltext_processed: true" in txt
        # Body invariants.
        assert "<!-- kb-fulltext-start -->" in txt
        assert "<!-- kb-fulltext-end -->" in txt
        assert "<!-- kb-ai-zone-start -->" in txt
        assert "<!-- kb-ai-zone-end -->" in txt
        # Body content preserved.
        assert "本章讨论平均模型" in txt
        # Original file was deleted.
        assert not (kb / "thoughts" / fname).exists()

    def test_body_preserved_byte_for_byte(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        body = (
            "*From [[papers/KEY|Title]], chapter 1.*\n\n"
            "## Sections\n\n"
            "- item with unicode: résumé 中文 🎯\n\n"
            "### Formulas\n\n"
            "$V_{DC} = \\sqrt{2} \\cdot 230V$\n\n"
            "A fenced block:\n\n"
            "```python\nprint('ok')\n```\n"
        )
        fname, text = _legacy_chapter_md(key="BOOKKEY5", chno=1, body=body)
        (kb / "thoughts" / fname).write_text(text)

        from kb_write.ops.migrate_chapters import migrate_legacy_chapters
        r = migrate_legacy_chapters(_ctx(kb))
        assert len(r.migrated) == 1

        new_txt = (kb / "papers" / "BOOKKEY5-ch01.md").read_text()
        # The exact unicode / formula / fenced-block content must
        # survive. Use simple substring checks rather than a full
        # diff since the surrounding frontmatter + zone markers
        # will differ by design.
        for needle in (
            "résumé 中文 🎯",
            "$V_{DC} = \\sqrt{2} \\cdot 230V$",
            "```python\nprint('ok')\n```",
            "*From [[papers/KEY|Title]], chapter 1.*",
        ):
            assert needle in new_txt, f"body content lost: {needle!r}"


class TestReport:
    def test_summary_counts_line(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb_root(tmp_path)
        # 2 to-migrate, 1 already-done, 1 collision.
        for key, chno in [("KEYAAAA1", 1), ("KEYAAAA1", 2)]:
            fname, text = _legacy_chapter_md(key=key, chno=chno)
            (kb / "thoughts" / fname).write_text(text)
        # Already done:
        fname, text = _legacy_chapter_md(key="KEYAAAA2", chno=3)
        (kb / "thoughts" / fname).write_text(text)
        (kb / "papers" / "KEYAAAA2-ch03.md").write_text(
            "---\nkind: paper\nzotero_key: KEYAAAA2\nchapter_number: 3\ntitle: x\n---\n"
        )
        # Collision:
        fname, text = _legacy_chapter_md(key="KEYAAAA3", chno=4)
        (kb / "thoughts" / fname).write_text(text)
        (kb / "papers" / "KEYAAAA3-ch04.md").write_text(
            "---\nkind: paper\nzotero_key: OTHER\nchapter_number: 99\ntitle: x\n---\n"
        )

        from kb_write.ops.migrate_chapters import (
            migrate_legacy_chapters, format_report,
        )
        r = migrate_legacy_chapters(_ctx(kb))
        assert len(r.migrated) == 2
        assert len(r.skipped_already_done) == 1
        assert len(r.skipped_collision) == 1

        rendered = format_report(r)
        assert "migrated 2 chapter(s)" in rendered
        assert "skipped 1 already-present" in rendered
        assert "1 collisions" in rendered
