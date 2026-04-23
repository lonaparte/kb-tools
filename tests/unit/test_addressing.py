"""Unit tests for kb_core.addressing — NodeAddress, parse_target,
from_md_path. These are the v26 path-layout enforcement points."""
from __future__ import annotations

from pathlib import Path

import pytest

from kb_core.addressing import NodeAddress, parse_target, from_md_path
from kb_core.paths import PathError


class TestNodeAddress:
    def test_paper_md_path(self):
        a = NodeAddress("paper", "ABCD")
        assert a.md_rel_path == "papers/ABCD.md"

    def test_thought_md_path(self):
        a = NodeAddress("thought", "2026-04-22-foo")
        assert a.md_rel_path == "thoughts/2026-04-22-foo.md"

    def test_topic_nested_key(self):
        a = NodeAddress("topic", "stability/small-signal")
        assert a.md_rel_path == "topics/agent-created/stability/small-signal.md"

    def test_note_md_path(self):
        a = NodeAddress("note", "zoterokey")
        assert a.md_rel_path == "topics/standalone-note/zoterokey.md"

    def test_preference_is_dot_agent_prefs(self):
        a = NodeAddress("preference", "writing-style")
        assert a.md_rel_path == ".agent-prefs/writing-style.md"

    def test_abspath(self, tmp_path):
        a = NodeAddress("paper", "X")
        assert a.md_abspath(tmp_path) == (tmp_path / "papers/X.md").resolve()


class TestParseTargetAccepts:
    def test_plural_paper(self):
        a = parse_target("papers/ABC")
        assert a == NodeAddress("paper", "ABC")

    def test_plural_paper_with_md(self):
        a = parse_target("papers/ABC.md")
        assert a == NodeAddress("paper", "ABC")

    def test_singular_paper(self):
        a = parse_target("paper/ABC")
        assert a == NodeAddress("paper", "ABC")

    def test_standalone_note(self):
        a = parse_target("topics/standalone-note/ZOTKEY")
        assert a == NodeAddress("note", "ZOTKEY")

    def test_agent_topic(self):
        a = parse_target("topics/agent-created/gfm")
        assert a == NodeAddress("topic", "gfm")

    def test_topic_with_nested_slug(self):
        a = parse_target("topics/agent-created/stability/overview")
        assert a == NodeAddress("topic", "stability/overview")

    def test_thought(self):
        a = parse_target("thoughts/2026-04-22-foo")
        assert a == NodeAddress("thought", "2026-04-22-foo")

    def test_trailing_slash_tolerated(self):
        a = parse_target("papers/ABC/")
        assert a == NodeAddress("paper", "ABC")

    def test_leading_slash_tolerated(self):
        a = parse_target("/papers/ABC")
        assert a == NodeAddress("paper", "ABC")


class TestParseTargetRejects:
    def test_empty(self):
        with pytest.raises(PathError):
            parse_target("")

    def test_whitespace_only(self):
        with pytest.raises(PathError):
            parse_target("   ")

    def test_no_subdir(self):
        with pytest.raises(PathError, match="no subdir prefix"):
            parse_target("just-a-key")

    def test_v25_zotero_notes_deprecated(self):
        # v26 refuses v25 path with a clear hint.
        with pytest.raises(PathError, match="DEPRECATED"):
            parse_target("zotero-notes/ABC")

    def test_v25_topics_top_level_deprecated(self):
        with pytest.raises(PathError, match="DEPRECATED"):
            parse_target("topics/mytopic")

    def test_unknown_subdir(self):
        with pytest.raises(PathError, match="unknown subdir"):
            parse_target("random/foo")

    def test_no_key_after_subdir(self):
        with pytest.raises(PathError):
            parse_target("papers/")

    def test_dotdot_in_key(self):
        # `..` in the key is a traversal attempt — reject.
        with pytest.raises(PathError, match="not allowed"):
            parse_target("papers/../etc")

    def test_dotdot_in_nested_topic(self):
        with pytest.raises(PathError, match="not allowed"):
            parse_target("topics/agent-created/a/../b")


class TestFromMdPath:
    def test_paper_round_trip(self, tmp_path):
        (tmp_path / "papers").mkdir()
        md = tmp_path / "papers" / "X.md"
        md.touch()
        a = from_md_path(tmp_path, md)
        assert a == NodeAddress("paper", "X")

    def test_thought(self, tmp_path):
        (tmp_path / "thoughts").mkdir()
        md = tmp_path / "thoughts" / "2026-04-22-n.md"
        md.touch()
        a = from_md_path(tmp_path, md)
        assert a == NodeAddress("thought", "2026-04-22-n")

    def test_agent_topic_with_nesting(self, tmp_path):
        (tmp_path / "topics" / "agent-created" / "a" / "b").mkdir(parents=True)
        md = tmp_path / "topics" / "agent-created" / "a" / "b" / "leaf.md"
        md.touch()
        a = from_md_path(tmp_path, md)
        assert a == NodeAddress("topic", "a/b/leaf")

    def test_standalone_note(self, tmp_path):
        (tmp_path / "topics" / "standalone-note").mkdir(parents=True)
        md = tmp_path / "topics" / "standalone-note" / "ZK.md"
        md.touch()
        a = from_md_path(tmp_path, md)
        assert a == NodeAddress("note", "ZK")

    def test_outside_kb_rejected(self, tmp_path):
        other = tmp_path.parent / "other"
        other.mkdir(exist_ok=True)
        with pytest.raises(PathError, match="outside"):
            from_md_path(tmp_path, other / "x.md")

    def test_v25_zotero_notes_diagnostic(self, tmp_path):
        (tmp_path / "zotero-notes").mkdir()
        md = tmp_path / "zotero-notes" / "old.md"
        md.touch()
        with pytest.raises(PathError, match="DEPRECATED"):
            from_md_path(tmp_path, md)

    def test_v25_top_level_topic_diagnostic(self, tmp_path):
        (tmp_path / "topics").mkdir()
        md = tmp_path / "topics" / "old.md"
        md.touch()
        with pytest.raises(PathError, match="DEPRECATED"):
            from_md_path(tmp_path, md)
