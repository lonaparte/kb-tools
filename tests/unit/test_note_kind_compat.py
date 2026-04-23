"""Tests for the v27 `kind: note` alias.

v25 review suggested `kind: zotero_standalone_note` is wordy and
inconsistent with the NodeAddress node_type ("note"). v27 writes
the shorter form for new mds; indexer and list_files accept both
so existing mds don't need migration.

These tests lock the accept-both behaviour so a future cleanup
doesn't silently break the 1000+ existing mds in real KBs."""
from __future__ import annotations

import pytest


def _skip_if_no_frontmatter():
    try:
        import frontmatter  # noqa: F401
    except ImportError:
        pytest.skip(
            "python-frontmatter not installed; md_builder / list "
            "require it"
        )


class TestMdBuilder:
    def test_new_notes_get_short_kind(self):
        """Fresh imports since v27 write the short form."""
        _skip_if_no_frontmatter()
        from kb_importer.md_builder import _build_note_frontmatter
        from kb_importer.zotero_reader import ZoteroItem, ZoteroNote

        # Build a standalone-note ZoteroItem with the real v0.27.x
        # dataclass shape. Previously this test constructed an
        # invented shape (extra=, pdf_attachment_key=, notes="str")
        # and silently TypeError'd inside the runner — reported in
        # v0.27.1 field testing as a CHANGELOG liability ("locked by
        # tests/unit/test_note_kind_compat.py" was fiction until
        # this fix).
        item = ZoteroItem(
            key="TESTKEY8",
            version=1,
            item_type="note",
            title="Test Note",
            authors=[],
            year=None,
            date="",
            publication="",
            doi="",
            url="",
            abstract="",
            citation_key="",
            tags=[],
            collections=[],
            date_added="2026-01-01",
            date_modified="2026-01-01",
            notes=[ZoteroNote(
                key="N1",
                version=1,
                parent_key=None,
                html="<p>body</p>",
                date_added="2026-01-01",
                date_modified="2026-01-01",
                tags=[],
            )],
            attachments=[],
        )
        fm = _build_note_frontmatter(item)
        assert fm["kind"] == "note", (
            "v27 regression: new notes should be written with "
            "`kind: note`, not the legacy long form"
        )


class TestListFilesKindFilter:
    """list_files with kind_filter='note' must find BOTH new-style
    (kind=note) and legacy (kind=zotero_standalone_note) notes."""

    def test_filter_note_matches_new_style(self, tmp_path):
        _skip_if_no_frontmatter()
        from kb_mcp.tools.list import list_files_impl

        (tmp_path / "topics" / "standalone-note").mkdir(parents=True)
        new = tmp_path / "topics" / "standalone-note" / "new.md"
        new.write_text('---\nkind: note\ntitle: new\n---\nbody\n')
        result = list_files_impl(
            tmp_path, subdir="topics/standalone-note",
            kind_filter="note",
        )
        assert "new.md" in result

    def test_filter_note_also_matches_legacy(self, tmp_path):
        _skip_if_no_frontmatter()
        from kb_mcp.tools.list import list_files_impl

        (tmp_path / "topics" / "standalone-note").mkdir(parents=True)
        legacy = tmp_path / "topics" / "standalone-note" / "legacy.md"
        legacy.write_text(
            '---\nkind: zotero_standalone_note\ntitle: old\n---\nbody\n'
        )
        result = list_files_impl(
            tmp_path, subdir="topics/standalone-note",
            kind_filter="note",
        )
        assert "legacy.md" in result, (
            "legacy `kind: zotero_standalone_note` must still be "
            "matched when filtering `kind=note` — otherwise 1000+ "
            "pre-v27 notes would appear to have vanished"
        )

    def test_filter_note_matches_both_together(self, tmp_path):
        _skip_if_no_frontmatter()
        from kb_mcp.tools.list import list_files_impl

        (tmp_path / "topics" / "standalone-note").mkdir(parents=True)
        (tmp_path / "topics" / "standalone-note" / "a.md").write_text(
            '---\nkind: note\ntitle: new\n---\nbody\n'
        )
        (tmp_path / "topics" / "standalone-note" / "b.md").write_text(
            '---\nkind: zotero_standalone_note\ntitle: old\n---\nbody\n'
        )
        result = list_files_impl(
            tmp_path, subdir="topics/standalone-note",
            kind_filter="note",
        )
        assert "a.md" in result
        assert "b.md" in result

    def test_filter_other_kind_still_exact(self, tmp_path):
        """Only `note` has the alias. Other kinds (paper, topic,
        thought) still exact-match so nothing unrelated leaks in."""
        _skip_if_no_frontmatter()
        from kb_mcp.tools.list import list_files_impl

        (tmp_path / "papers").mkdir()
        (tmp_path / "papers" / "P.md").write_text(
            '---\nkind: paper\ntitle: p\n---\nbody\n'
        )
        (tmp_path / "papers" / "mystery.md").write_text(
            '---\nkind: zotero_standalone_note\ntitle: m\n---\nbody\n'
        )
        # Filtering for "paper" must NOT pick up the legacy note.
        result = list_files_impl(
            tmp_path, subdir="papers", kind_filter="paper",
        )
        assert "P.md" in result
        assert "mystery.md" not in result
