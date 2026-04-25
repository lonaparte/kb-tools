"""1.4.6: regression test for the linker fallback non-atomic write.

Pre-1.4.6 `linker._link_edges_to_kb_mcp_db` wrote citation-edges.jsonl
via streaming `open(path, "w") + .write()` per-line. Process
interruption mid-loop left a partial file. Post-fix uses
`_atomic_write_text` (tempfile + fsync + os.replace).

Two cases:
  1. Successful fallback writes a complete file with all edges.
  2. Crash mid-write leaves either no file or the prior file —
     never a half-populated one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_citations.linker import _atomic_write_text


def test_atomic_write_text_creates_file(tmp_path: Path):
    out = tmp_path / "edges.jsonl"
    payload = '{"a": 1}\n{"b": 2}\n'
    _atomic_write_text(out, payload)
    assert out.read_text() == payload


def test_atomic_write_text_overwrites_atomically(tmp_path: Path):
    """Replacement leaves the file fully old or fully new — never
    half-merged or with mixed content."""
    out = tmp_path / "edges.jsonl"
    out.write_text("OLD\n")
    _atomic_write_text(out, "NEW1\nNEW2\n")
    assert out.read_text() == "NEW1\nNEW2\n"


def test_atomic_write_text_failure_leaves_target_intact(tmp_path: Path, monkeypatch):
    """If the rename step raises, the original target stays untouched
    and the temp file is cleaned up."""
    import os as _os
    out = tmp_path / "edges.jsonl"
    out.write_text("OLD\n")

    real_replace = _os.replace
    def fake_replace(src, dst):
        raise OSError("simulated crash")
    monkeypatch.setattr(_os, "replace", fake_replace)

    with pytest.raises(OSError):
        _atomic_write_text(out, "NEW\n")

    # Target unchanged.
    assert out.read_text() == "OLD\n"
    # No tempfile residue.
    leftovers = list(tmp_path.glob(".edges.jsonl.*"))
    assert leftovers == [], f"tempfile leaked: {leftovers}"


def test_atomic_write_creates_parent_directory(tmp_path: Path):
    """Parent dir doesn't exist → atomic write creates it."""
    out = tmp_path / "subdir" / "edges.jsonl"
    _atomic_write_text(out, "x\n")
    assert out.read_text() == "x\n"


def test_atomic_write_empty_payload(tmp_path: Path):
    """Edge case: zero bytes — must still produce an empty file
    (not omit the file entirely)."""
    out = tmp_path / "empty.jsonl"
    _atomic_write_text(out, "")
    assert out.exists()
    assert out.read_bytes() == b""
