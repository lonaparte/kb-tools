"""Regression for the v0.29.0 _fetch_children error propagation.

Bug: before 0.29, zotero_reader._fetch_children caught every
exception from self._z.children() and returned ([], []). Any
transient Zotero API failure (network, rate limit, auth blip)
made papers with real PDFs appear attachment-less. The import
pipeline then wrote the paper md with:
    zotero_attachment_keys: []
    zotero_max_child_version: 0

On the next successful run, those values swung back to the real
state. The symptom: papers oscillating between "has-PDF" and
"no-PDF" in the KB, storage/_archived/ being shuffled, md mtimes
churning on every sync.

Fix: _fetch_children raises ZoteroChildrenFetchError. The
top-level import loop catches this specific error, logs "paper
skipped, md unchanged", and moves on.

These tests use a stub pyzotero.Zotero whose children() raises
on demand.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from conftest import skip_if_no_pyzotero


def test_fetch_children_raises_on_api_failure():
    skip_if_no_pyzotero()
    from kb_importer.zotero_reader import (
        ZoteroReader, ZoteroChildrenFetchError,
    )
    import pytest

    # Build a reader with a stubbed pyzotero client.
    reader = ZoteroReader.__new__(ZoteroReader)
    reader._z = MagicMock()
    reader._z.children.side_effect = ConnectionError("network blip")

    with pytest.raises(ZoteroChildrenFetchError) as exc:
        reader._fetch_children("ABCD1234")
    assert exc.value.parent_key == "ABCD1234"
    assert "network blip" in str(exc.value)
    assert "ConnectionError" in str(exc.value)


def test_fetch_children_success_still_returns_empty_when_no_children():
    """Legitimate no-children case: the API returned []. Must NOT
    be confused with fetch failure. Pre-0.29 both paths looked
    identical; now they're distinct."""
    skip_if_no_pyzotero()
    from kb_importer.zotero_reader import ZoteroReader

    reader = ZoteroReader.__new__(ZoteroReader)
    reader._z = MagicMock()
    reader._z.children.return_value = []   # legit empty

    notes, attachments = reader._fetch_children("ABCD1234")
    assert notes == []
    assert attachments == []


def test_fetch_children_distinguishes_pdf_from_non_pdf():
    """Sanity: the filter still works — non-PDF attachments are
    dropped, notes are kept separately, etc."""
    skip_if_no_pyzotero()
    from kb_importer.zotero_reader import ZoteroReader

    reader = ZoteroReader.__new__(ZoteroReader)
    reader._z = MagicMock()
    reader._z.children.return_value = [
        {"key": "N1", "version": 5, "data": {
            "itemType": "note", "note": "<p>hi</p>",
        }},
        {"key": "ATT1", "version": 10, "data": {
            "itemType": "attachment", "contentType": "application/pdf",
            "filename": "paper.pdf", "linkMode": "imported_file",
        }},
        # Non-PDF attachment: must be dropped.
        {"key": "ATT2", "version": 11, "data": {
            "itemType": "attachment", "contentType": "text/html",
            "filename": "snapshot.html", "linkMode": "imported_url",
        }},
    ]

    notes, attachments = reader._fetch_children("PARENT01")
    assert len(notes) == 1
    assert notes[0].key == "N1"
    assert len(attachments) == 1
    assert attachments[0].key == "ATT1"
