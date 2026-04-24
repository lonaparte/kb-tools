"""Regression for the v0.29.0 auto-archive removal.

Pre-0.29: after a successful paper import, _process_paper moved
every attachment dir from storage/{KEY}/ to storage/_archived/{KEY}/.
Combined with the _fetch_children exception-swallow bug (also fixed
in 0.29), this caused attachment dirs to bounce between the two
locations on every transient Zotero API blip.

v0.29 removed the auto-archive step. This test asserts that
running _process_paper on a paper with a PDF leaves storage/
unchanged — the PDF stays where it is.

archive_attachments() itself becomes a no-op DeprecationWarning.
"""
from __future__ import annotations

import pathlib
import pytest
import warnings

from conftest import skip_if_no_pyzotero


def test_archive_attachments_is_now_noop(tmp_path, monkeypatch):
    skip_if_no_pyzotero()
    from kb_importer.config import Config
    from kb_importer.state import archive_attachments, ArchiveResult

    cfg = Config(
        zotero_storage_dir=tmp_path / "storage",
        kb_root=tmp_path / "kb",
    )
    cfg.zotero_storage_dir.mkdir()
    src = cfg.zotero_storage_dir / "ABCD1234"
    src.mkdir()
    (src / "paper.pdf").write_bytes(b"%PDF-1.4 fake")

    # Calling archive_attachments should NOT move the file, and
    # should emit a DeprecationWarning.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = archive_attachments(cfg, ["ABCD1234"])
    # File still in storage/, not in _archived/.
    assert src.exists(), "src was moved; archive should now be a no-op"
    assert not (cfg.zotero_storage_dir / "_archived" / "ABCD1234").exists()
    # Deprecation warning fired.
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        f"expected DeprecationWarning, got: {[w.message for w in caught]}"
    )
    # Result is empty-success shape.
    assert result.moved == []
    assert result.not_found == ["ABCD1234"]


def test_archive_attachments_noop_for_empty_list():
    """Empty input: don't even bother with the deprecation warning."""
    skip_if_no_pyzotero()
    from kb_importer.config import Config
    from kb_importer.state import archive_attachments
    import pathlib

    cfg = Config(zotero_storage_dir=pathlib.Path("/tmp/nope"),
                  kb_root=pathlib.Path("/tmp/nope"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = archive_attachments(cfg, [])
    # No DeprecationWarning for the vacuous case — noisy otherwise.
    dep_warnings = [w for w in caught
                     if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings == []
    assert result.moved == []
    assert result.not_found == []


def test_unarchive_attachments_still_functional(tmp_path):
    """unarchive_attachments is KEPT as a migration helper for
    operators whose pre-0.29 installations have PDFs under
    _archived/. This test locks its behaviour so we don't
    accidentally no-op it too."""
    skip_if_no_pyzotero()
    from kb_importer.config import Config
    from kb_importer.state import unarchive_attachments

    cfg = Config(
        zotero_storage_dir=tmp_path / "storage",
        kb_root=tmp_path / "kb",
    )
    archive = cfg.zotero_storage_dir / "_archived"
    archive.mkdir(parents=True)
    src = archive / "ABCD1234"
    src.mkdir()
    (src / "paper.pdf").write_bytes(b"%PDF-1.4")

    result = unarchive_attachments(cfg, ["ABCD1234"])

    # File moved from _archived/ to storage/.
    assert not src.exists()
    assert (cfg.zotero_storage_dir / "ABCD1234" / "paper.pdf").exists()
    assert result.moved == ["ABCD1234"]


def test_find_pdf_still_resolves_archived(tmp_path):
    """0.29 removed auto-archive but find_pdf still resolves
    _archived/ for back-compat with pre-0.29 installations that
    already have PDFs there."""
    skip_if_no_pyzotero()
    from kb_importer.config import Config
    from kb_importer.state import find_pdf

    cfg = Config(
        zotero_storage_dir=tmp_path / "storage",
        kb_root=tmp_path / "kb",
    )
    archive = cfg.zotero_storage_dir / "_archived" / "LEGACY01"
    archive.mkdir(parents=True)
    (archive / "paper.pdf").write_bytes(b"%PDF-1.4")

    pdf, is_archived = find_pdf(cfg, "LEGACY01")
    assert pdf is not None
    assert pdf.name == "paper.pdf"
    assert is_archived is True
