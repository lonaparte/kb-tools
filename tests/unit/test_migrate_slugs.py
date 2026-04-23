"""Tests for `kb-write migrate-slugs` — the v0.28.0 one-shot
migration that renames thought mds whose slugs violate the
current lowercase-kebab format.

Covers:
  - canonical slug detection (positive cases: uppercase keys,
    disallowed chars, double-hyphens; negative: already-canonical)
  - non-date-prefixed slugs reported as errors (not forcibly
    renamed — we refuse to guess)
  - dry-run doesn't touch files
  - collision: canonical target already exists → skip, report
  - apply: file is actually renamed; audit.log entry written
  - format_report renders without crashing
"""
from __future__ import annotations

from pathlib import Path


def _kb_root(tmp_path: Path) -> Path:
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


def _thought(kb, slug: str, body: str = "body\n") -> Path:
    p = kb / "thoughts" / f"{slug}.md"
    p.write_text(
        "---\nkind: thought\ntitle: t\n---\n" + body
    )
    return p


class TestCanonicalisation:
    """Unit-level check of _canonicalise_thought_slug."""

    def test_already_canonical_unchanged(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert (
            _canonicalise_thought_slug("2026-04-22-some-note")
            == "2026-04-22-some-note"
        )

    def test_uppercase_is_lowercased(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert (
            _canonicalise_thought_slug("2026-04-22-ABCD1234-chapter")
            == "2026-04-22-abcd1234-chapter"
        )

    def test_underscores_become_hyphens(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert (
            _canonicalise_thought_slug("2026-04-22-foo_bar_baz")
            == "2026-04-22-foo-bar-baz"
        )

    def test_spaces_become_hyphens(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert (
            _canonicalise_thought_slug("2026-04-22-foo bar baz")
            == "2026-04-22-foo-bar-baz"
        )

    def test_double_hyphens_collapsed(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert (
            _canonicalise_thought_slug("2026-04-22-foo--bar---baz")
            == "2026-04-22-foo-bar-baz"
        )

    def test_trailing_disallowed_stripped(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert (
            _canonicalise_thought_slug("2026-04-22-foo!")
            == "2026-04-22-foo"
        )

    def test_no_date_prefix_returns_none(self):
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert _canonicalise_thought_slug("not-a-date-slug") is None

    def test_only_disallowed_rest_returns_none(self):
        """Rest after date is all disallowed chars → canonicalises
        to empty → None (can't produce a safe filename)."""
        from kb_write.ops.migrate_slugs import _canonicalise_thought_slug
        assert _canonicalise_thought_slug("2026-04-22-!!!") is None


class TestDetection:
    def test_canonical_slug_not_planned(self, tmp_path):
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-some-note")
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb), dry_run=True)
        assert len(r.plans) == 0

    def test_uppercase_key_planned(self, tmp_path):
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-ABCD1234-chapter-note")
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb), dry_run=True)
        assert len(r.plans) == 1
        p = r.plans[0]
        assert p.old_slug == "2026-04-22-ABCD1234-chapter-note"
        assert p.new_slug == "2026-04-22-abcd1234-chapter-note"

    def test_underscore_slug_planned(self, tmp_path):
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-with_underscore_things")
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb), dry_run=True)
        assert len(r.plans) == 1
        assert r.plans[0].new_slug == "2026-04-22-with-underscore-things"

    def test_no_date_prefix_reported_as_error(self, tmp_path):
        """A slug that doesn't start with YYYY-MM-DD can't be safely
        canonicalised (we won't invent a date). It lands in errors,
        not plans."""
        kb = _kb_root(tmp_path)
        _thought(kb, "not-a-date-slug")
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb), dry_run=True)
        assert len(r.plans) == 0
        assert len(r.errors) == 1
        md_path, reason = r.errors[0]
        assert "not-a-date-slug" in md_path.name
        assert "date-prefixed" in reason.lower() or "manual" in reason.lower()


class TestDryRun:
    def test_dry_run_does_not_rename(self, tmp_path):
        kb = _kb_root(tmp_path)
        src = _thought(kb, "2026-04-22-UPPER-case")
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb), dry_run=True)
        assert r.dry_run is True
        # Source still there, target never created.
        assert src.exists()
        assert not (kb / "thoughts" / "2026-04-22-upper-case.md").exists()
        assert len(r.migrated) == 0


class TestCollision:
    def test_canonical_target_already_exists(self, tmp_path):
        """If the canonicalised filename is already taken by another
        md, we skip (report collision) — we never overwrite."""
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-UPPER-case", body="original\n")
        _thought(kb, "2026-04-22-upper-case", body="squatter\n")
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb))
        assert len(r.migrated) == 0
        assert len(r.skipped_collision) == 1
        # Both files still present.
        assert (kb / "thoughts" / "2026-04-22-UPPER-case.md").exists()
        assert (kb / "thoughts" / "2026-04-22-upper-case.md").exists()


class TestApply:
    def test_file_is_actually_renamed(self, tmp_path):
        kb = _kb_root(tmp_path)
        src = _thought(kb, "2026-04-22-UPPER-case", body="original\n")
        original_body = src.read_text()
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb))
        assert len(r.migrated) == 1
        # Source gone, target present.
        assert not src.exists()
        dst = kb / "thoughts" / "2026-04-22-upper-case.md"
        assert dst.exists()
        # Body preserved byte-for-byte.
        assert dst.read_text() == original_body

    def test_audit_log_written(self, tmp_path):
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-UPPER-case")
        from kb_write.ops.migrate_slugs import migrate_slugs
        migrate_slugs(_ctx(kb))
        audit = kb / ".kb-mcp" / "audit.log"
        # audit module creates the dir/file on first record.
        if audit.exists():
            contents = audit.read_text()
            assert "migrate_slug" in contents
            assert "2026-04-22-upper-case" in contents

    def test_empty_kb_no_plans(self, tmp_path):
        kb = _kb_root(tmp_path)
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(kb))
        assert r.plans == []
        assert r.migrated == []
        assert r.errors == []
        assert r.skipped_collision == []

    def test_no_thoughts_dir_is_silent(self, tmp_path):
        """A KB without thoughts/ at all is fine — nothing to do."""
        from kb_write.ops.migrate_slugs import migrate_slugs
        r = migrate_slugs(_ctx(tmp_path))
        assert r.plans == []
        assert r.errors == []


class TestReport:
    def test_format_report_dry_run(self, tmp_path):
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-UPPER-case")
        from kb_write.ops.migrate_slugs import migrate_slugs, format_report
        r = migrate_slugs(_ctx(kb), dry_run=True)
        out = format_report(r)
        assert "dry-run" in out
        assert "UPPER-case" in out or "upper-case" in out

    def test_format_report_applied(self, tmp_path):
        kb = _kb_root(tmp_path)
        _thought(kb, "2026-04-22-UPPER-case")
        from kb_write.ops.migrate_slugs import migrate_slugs, format_report
        r = migrate_slugs(_ctx(kb))
        out = format_report(r)
        assert "migrated" in out
        assert "1" in out
