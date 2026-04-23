"""Unit tests for kb_mcp.tools.snapshot — export/import round-trip.

In particular v27 requires:
- Manifest does NOT contain kb_root_at_export (host path leak).
- Manifest version bumped to 2.
- Import accepts both v1 (old) and v2 (new) manifests."""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from kb_mcp.tools.snapshot import (
    export_snapshot, import_snapshot, _MANIFEST_VERSION, _MANIFEST_REL,
)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    """A minimal KB with a real (but nearly empty) SQLite DB so
    snapshot.export_snapshot has something to archive."""
    (tmp_path / "papers").mkdir()
    (tmp_path / ".kb-mcp").mkdir()
    # Create a real SQLite file — export uses the online-backup API
    # which needs an actual SQLite database, not arbitrary bytes.
    import sqlite3
    db_path = tmp_path / ".kb-mcp" / "index.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS marker(x INT)")
    conn.execute("INSERT INTO marker VALUES (42)")
    conn.commit()
    conn.close()
    return tmp_path


def test_manifest_version_is_2():
    # v26→v27 bump: any change of _MANIFEST_VERSION is coordination-
    # breaking; regression gate.
    assert _MANIFEST_VERSION == 2


def test_manifest_does_not_contain_kb_root(kb, tmp_path):
    """Security: v27 removed kb_root_at_export from the manifest.
    Exporting a snapshot must not leak the host's kb_root path."""
    out = tmp_path / "snap.tar"
    export_snapshot(kb, out)

    with tarfile.open(out) as tf:
        member = tf.getmember(_MANIFEST_REL)
        f = tf.extractfile(member)
        assert f is not None
        manifest = json.loads(f.read().decode("utf-8"))

    assert "kb_root_at_export" not in manifest, (
        "regression: manifest still leaks kb_root — would leak the "
        "exporter's home dir layout to any snapshot recipient"
    )
    # Fields that SHOULD be there.
    assert manifest["manifest_version"] == 2
    assert "created_at" in manifest
    assert "includes" in manifest


def test_round_trip(kb, tmp_path):
    """Export a snapshot, import it elsewhere, confirm content
    matches."""
    out = tmp_path / "snap.tar"
    result = export_snapshot(kb, out)
    assert out.is_file()
    assert result["size_bytes"] > 0

    # Import into a fresh empty KB.
    fresh = tmp_path / "fresh-kb"
    fresh.mkdir()
    (fresh / ".kb-mcp").mkdir()
    import_snapshot(fresh, out, force=True)

    # index.sqlite must be back, and still contain our marker row.
    restored = fresh / ".kb-mcp" / "index.sqlite"
    assert restored.is_file()
    import sqlite3
    conn = sqlite3.connect(str(restored))
    try:
        row = conn.execute("SELECT x FROM marker").fetchone()
        assert row[0] == 42
    finally:
        conn.close()
