"""Tests for `kb-write doctor --fix` — v0.28.0 adds list-field
duplicate removal as a high-confidence auto-fix, joining scaffold
file creation and empty-AI-zone marker append.

Focus: the NEW `_check_list_duplicates` / dedup behaviour. Existing
A/B/C fixes are exercised indirectly by the integration suite.
"""
from __future__ import annotations

from pathlib import Path

from conftest import skip_if_no_frontmatter


def _ctx(kb_root):
    from kb_write.config import WriteContext
    return WriteContext(
        kb_root=kb_root,
        git_commit=False, reindex=False, lock=False,
        dry_run=False,
    )


def _kb(tmp_path: Path) -> Path:
    (tmp_path / "papers").mkdir()
    (tmp_path / "thoughts").mkdir()
    (tmp_path / "topics" / "standalone-note").mkdir(parents=True)
    (tmp_path / "topics" / "agent-created").mkdir(parents=True)
    return tmp_path


def _paper(kb: Path, key: str, *, kb_tags=None, kb_refs=None, authors=None) -> Path:
    parts = ["---", "kind: paper", "title: t"]
    if kb_tags is not None:
        parts.append(f"kb_tags: {kb_tags!r}".replace("'", '"'))
    if kb_refs is not None:
        parts.append(f"kb_refs: {kb_refs!r}".replace("'", '"'))
    if authors is not None:
        parts.append(f"authors: {authors!r}".replace("'", '"'))
    parts += ["---", "body", ""]
    p = kb / "papers" / f"{key}.md"
    p.write_text("\n".join(parts))
    return p


class TestDetection:
    def test_kb_tags_dup_reported(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        _paper(kb, "P1", kb_tags=["a", "b", "a"])
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb))
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert len(dupes) == 1
        assert dupes[0].path == "papers/P1.md"
        assert "kb_tags" in dupes[0].message
        assert "'a'" in dupes[0].message
        assert dupes[0].auto_fixable is True

    def test_kb_refs_dup_reported(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        _paper(kb, "P1", kb_refs=["papers/X", "papers/X", "papers/Y"])
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb))
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert len(dupes) == 1
        assert "kb_refs" in dupes[0].message

    def test_authors_dup_reported(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        _paper(kb, "P1", authors=["Ada", "Bob", "Ada"])
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb))
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert len(dupes) == 1
        assert "authors" in dupes[0].message

    def test_no_dup_no_finding(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        _paper(kb, "P1", kb_tags=["a", "b", "c"], kb_refs=["papers/X"])
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb))
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert dupes == []

    def test_empty_list_no_finding(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        _paper(kb, "P1", kb_tags=[])
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb))
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert dupes == []

    def test_malformed_list_skipped(self, tmp_path):
        """Non-list kb_tags (e.g. a string by YAML typo) should NOT be
        touched by the dedup check — it's the type-check's job to
        report that. Avoid double-reporting."""
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        (kb / "papers" / "P1.md").write_text(
            '---\nkind: paper\ntitle: t\nkb_tags: "string-not-list"\n---\nbody\n'
        )
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb))
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert dupes == []


class TestFixApplied:
    def test_fix_rewrites_deduped(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        md = _paper(kb, "P1", kb_tags=["a", "b", "a", "c", "b"])
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb), fix=True)
        assert any(
            "deduped kb_tags" in f for f in r.fixed
        ), f"expected dedupe in fixed list, got: {r.fixed}"
        # File rewritten: kb_tags now [a, b, c] (first-occurrence order).
        import frontmatter
        post = frontmatter.load(str(md))
        assert post.metadata["kb_tags"] == ["a", "b", "c"]

    def test_fix_preserves_other_fields(self, tmp_path):
        """Dedup must not touch title, kind, other non-duplicated
        fields, or body."""
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        md = kb / "papers" / "P1.md"
        md.write_text(
            "---\n"
            "kind: paper\n"
            'title: "A Title"\n'
            "year: 2024\n"
            'kb_tags: ["dup", "dup", "unique"]\n'
            "---\n"
            "## Body heading\n\n"
            "line one\nline two\n"
        )
        from kb_write.ops.doctor import doctor
        doctor(_ctx(kb), fix=True)
        import frontmatter
        post = frontmatter.load(str(md))
        assert post.metadata["kind"] == "paper"
        assert post.metadata["title"] == "A Title"
        assert post.metadata["year"] == 2024
        assert post.metadata["kb_tags"] == ["dup", "unique"]
        # Body preserved.
        assert "Body heading" in post.content
        assert "line one" in post.content

    def test_fix_handles_multiple_fields_in_one_file(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        md = _paper(
            kb, "P1",
            kb_tags=["a", "a"],
            kb_refs=["papers/X", "papers/X"],
            authors=["Ada", "Ada"],
        )
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb), fix=True)
        # Three separate findings, one per field.
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert len(dupes) == 3
        import frontmatter
        post = frontmatter.load(str(md))
        assert post.metadata["kb_tags"] == ["a"]
        assert post.metadata["kb_refs"] == ["papers/X"]
        assert post.metadata["authors"] == ["Ada"]

    def test_no_fix_without_flag(self, tmp_path):
        """Without --fix, findings are reported but file is unchanged."""
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        md = _paper(kb, "P1", kb_tags=["a", "a"])
        original = md.read_text()
        from kb_write.ops.doctor import doctor
        r = doctor(_ctx(kb), fix=False)
        dupes = [f for f in r.findings if f.category == "list-duplicates"]
        assert len(dupes) == 1
        assert r.fixed == []
        # File unchanged byte-for-byte.
        assert md.read_text() == original


class TestOrderPreservation:
    def test_first_occurrence_wins(self, tmp_path):
        skip_if_no_frontmatter()
        kb = _kb(tmp_path)
        md = _paper(kb, "P1", kb_tags=["z", "a", "z", "m", "a"])
        from kb_write.ops.doctor import doctor
        doctor(_ctx(kb), fix=True)
        import frontmatter
        post = frontmatter.load(str(md))
        assert post.metadata["kb_tags"] == ["z", "a", "m"]
