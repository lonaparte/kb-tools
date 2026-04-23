"""Tests for kb_core.format — render_path, render_error, render_json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_core.format import (
    render_path,
    render_error,
    render_json,
    WRITE_RESULT_FIELD_ORDER,
)


class TestRenderPath:
    def test_relative_default(self, tmp_path):
        (tmp_path / "papers").mkdir()
        p = tmp_path / "papers" / "X.md"
        assert render_path(p, tmp_path) == "papers/X.md"

    def test_absolute_flag(self, tmp_path):
        (tmp_path / "papers").mkdir()
        p = tmp_path / "papers" / "X.md"
        assert render_path(p, tmp_path, absolute=True) == str(p)

    def test_no_kb_root_returns_str(self, tmp_path):
        p = tmp_path / "papers" / "X.md"
        assert render_path(p, None) == str(p)

    def test_path_outside_root_fallbacks(self, tmp_path):
        outside = tmp_path.parent / "x.md"
        # Should not raise even though the path can't be made relative.
        result = render_path(outside, tmp_path)
        assert isinstance(result, str)

    def test_accepts_string_input(self, tmp_path):
        (tmp_path / "papers").mkdir()
        assert render_path(
            str(tmp_path / "papers" / "X.md"), tmp_path,
        ) == "papers/X.md"


class TestRenderError:
    def test_default_prefix(self):
        assert render_error("boom") == "error: boom"

    def test_custom_prefix(self):
        assert render_error("failed", prefix="✗") == "✗: failed"

    def test_with_code(self):
        s = render_error("quota hit", code="quota")
        assert "quota hit" in s
        assert "[code=quota]" in s

    def test_without_code_no_trailing_bracket(self):
        assert "[code=" not in render_error("boom")


class TestRenderJson:
    def test_basic_ordering(self):
        payload = {"key": "X", "node_type": "paper", "mtime": 1.0}
        out = render_json(payload, WRITE_RESULT_FIELD_ORDER)
        parsed = json.loads(out)
        # node_type should come before key (per WRITE_RESULT_FIELD_ORDER)
        keys = list(parsed)
        assert keys.index("node_type") < keys.index("key")

    def test_unknown_fields_appended_sorted(self):
        payload = {"zzz": 1, "node_type": "paper", "aaa": 2}
        out = render_json(payload, WRITE_RESULT_FIELD_ORDER)
        parsed = json.loads(out)
        keys = list(parsed)
        assert keys[0] == "node_type"       # ordered first
        assert keys[1:] == ["aaa", "zzz"]   # extras sorted

    def test_missing_fields_skipped(self):
        """Fields in `field_order` but not in payload should be
        silently dropped — otherwise every new optional column
        would force updates everywhere."""
        payload = {"key": "X"}
        out = render_json(payload, WRITE_RESULT_FIELD_ORDER)
        parsed = json.loads(out)
        assert list(parsed) == ["key"]

    def test_round_trip(self):
        payload = {
            "node_type": "paper", "key": "ABC",
            "md_path": "papers/ABC.md", "mtime": 123.456,
            "reindexed": True, "custom_field": "x",
        }
        out = render_json(payload, WRITE_RESULT_FIELD_ORDER)
        assert json.loads(out) == payload  # all values preserved

    def test_non_ascii_preserved(self):
        """render_json uses ensure_ascii=False so CJK, quotes, and
        other non-ASCII chars don't get escaped — makes output
        legible in terminals / issue trackers."""
        payload = {"detail": "摘要中文"}
        out = render_json(payload)
        assert "摘要" in out
