"""Zotero data reader.

Two source modes (selected via Config):
- "live": Zotero 7 local HTTP API at localhost:23119 (requires Zotero
  to be running on the same host).
- "web":  Zotero cloud web API at api.zotero.org (requires library_id
  and an API key).

Both modes use pyzotero under the hood, which returns identical JSON
structure for both — so the per-item logic here is mode-agnostic.

This module translates raw Zotero items into plain Python dataclasses
with a stable shape, so the rest of the codebase doesn't need to know
anything about pyzotero internals.

TODO: A future "sqlite" mode could read zotero.sqlite directly for
fully offline operation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from pyzotero import zotero


log = logging.getLogger(__name__)


class ZoteroSourceError(Exception):
    """Raised when the configured Zotero source cannot be used
    (missing API key, invalid mode, etc.)."""


# Zotero item types we consider "papers" (generate papers/{key}.md).
# Anything note/attachment is handled specially.
PAPER_ITEM_TYPES = {
    "journalArticle",
    "conferencePaper",
    "book",
    "bookSection",
    "thesis",
    "preprint",
    "report",
    "magazineArticle",
    "newspaperArticle",
    "encyclopediaArticle",
    "manuscript",
    "webpage",
    "document",
}


@dataclass
class ZoteroItem:
    """Normalized view of a Zotero item.

    Keep this deliberately flat and string-y — consumers (md builder)
    shouldn't need to understand Zotero's schema quirks.
    """

    key: str
    version: int
    item_type: str
    title: str
    authors: list[str]
    year: int | None
    date: str
    publication: str
    doi: str
    url: str
    abstract: str
    citation_key: str
    tags: list[str]
    collections: list[str]       # resolved names, not keys
    date_added: str
    date_modified: str
    # For papers, child notes (already HTML); for standalone notes, the
    # own HTML content lives here as a single element.
    notes: list[ZoteroNote]
    # PDF attachments belonging to this paper. Each has its own Zotero
    # item key (NOT the paper's key!) which is also the name of its
    # subdirectory under ~/Zotero/storage/. A paper often has more than
    # one (main PDF + supplementary PDF + annotated copy from ZotFile).
    # Empty list for standalone notes.
    attachments: list[ZoteroAttachment]


@dataclass
class ZoteroNote:
    """A Zotero note (either child of a paper or standalone).

    `version` is Zotero's per-item version number. Editing a child note
    bumps this number but NOT the parent paper's version, so sync must
    check child note versions independently.
    """

    key: str
    version: int
    parent_key: str | None
    html: str
    date_added: str
    date_modified: str
    tags: list[str]


@dataclass
class ZoteroAttachment:
    """A PDF attachment child of a paper item.

    Zotero storage/ subdirectories are named after ATTACHMENT keys, not
    paper keys. A single paper can have multiple attachments (main PDF,
    supplements, etc.), each with its own key/dir. To locate an
    attachment's PDF on disk: `cfg.zotero_storage_dir / key / filename`.

    `filename` is the name Zotero assigned inside the storage dir (e.g.
    "Smith et al. - 2024 - Title.pdf"). It comes from the attachment's
    `filename` field (pyzotero's `data.filename`). Only PDFs are
    tracked; other content types (HTML snapshots, images, etc.) are
    filtered out at collection time.
    """

    key: str
    version: int
    parent_key: str
    filename: str             # e.g. "Smith_2024.pdf"
    content_type: str         # always "application/pdf" in Phase 1
    date_added: str
    date_modified: str


class ZoteroReader:
    """Thin wrapper around pyzotero.Zotero.

    Supports two modes, selected via Config:

    - "live": local HTTP API at localhost:23119. Requires Zotero to be
      running. library_id/library_type/api_key are ignored (pyzotero
      local mode uses dummy values).
    - "web": cloud web API (api.zotero.org). Requires library_id and
      an API key read from an env var (name specified in config).

    The rest of the class (get_paper, children, etc.) is mode-agnostic
    because pyzotero returns identical JSON structure for both.
    """

    def __init__(self, cfg=None) -> None:
        """Construct from a Config object.

        The `cfg` parameter is typed loosely (not annotated as Config)
        to avoid a circular import between config.py and zotero_reader.py.
        Callers should pass a kb_importer.config.Config instance.

        If `cfg` is None, falls back to live mode (useful for tests).
        """
        if cfg is None:
            # Backward-compat default for any caller that hasn't been
            # updated to pass cfg yet (and for tests).
            self._z = zotero.Zotero(
                library_id="0", library_type="user", local=True,
            )
            self._mode = "live"
            self._collection_name_cache = {}
            return

        self._mode = cfg.zotero_source_mode

        if self._mode == "web":
            import os
            api_key = os.environ.get(cfg.zotero_api_key_env, "").strip()
            if not api_key:
                raise ZoteroSourceError(
                    f"Zotero web mode requires an API key in environment "
                    f"variable {cfg.zotero_api_key_env!r}, but it is not "
                    f"set or empty. Generate one at "
                    f"https://www.zotero.org/settings/keys (read-only is "
                    f"sufficient) and export it."
                )
            self._z = zotero.Zotero(
                library_id=cfg.zotero_library_id,
                library_type=cfg.zotero_library_type,
                api_key=api_key,
            )
        elif self._mode == "live":
            self._z = zotero.Zotero(
                library_id="0",
                library_type="user",
                local=True,
            )
        else:
            # Validated by load_config, but double-check defensively.
            raise ZoteroSourceError(
                f"Unsupported zotero source mode: {self._mode!r}"
            )

        self._collection_name_cache: dict[str, str] = {}

    @property
    def mode(self) -> str:
        """Current source mode ('live' or 'web'). For diagnostics."""
        return self._mode

    def ping(self) -> None:
        """Cheap connectivity check.

        Makes exactly ONE request: fetch up to 1 top-level item. That's
        enough to verify:
          - host is reachable
          - API key (in web mode) is valid
          - local API is running (in live mode)

        Raises whatever pyzotero raises on failure; returns silently on
        success. Does NOT page through the library — unlike list_paper_keys
        which can take ~70s on a 1000+ paper library in web mode.
        """
        # pyzotero's top() accepts limit via kwargs.
        self._z.top(limit=1)

    # ------------------------------------------------------------------
    # Top-level listings
    # ------------------------------------------------------------------

    def list_paper_keys(self) -> set[str]:
        """All top-level items that are paper-like (not notes)."""
        keys: set[str] = set()
        for item in self._iter_everything(self._z.top()):
            data = item.get("data", {})
            if data.get("itemType") in PAPER_ITEM_TYPES:
                keys.add(item["key"])
        return keys

    def list_standalone_note_keys(self) -> set[str]:
        """All top-level notes (itemType=note, no parent)."""
        keys: set[str] = set()
        for item in self._iter_everything(self._z.top()):
            data = item.get("data", {})
            if data.get("itemType") == "note" and not data.get("parentItem"):
                keys.add(item["key"])
        return keys

    # ------------------------------------------------------------------
    # Fulltext (attachment-level)
    # ------------------------------------------------------------------

    def fetch_fulltext(self, attachment_key: str) -> str | None:
        """Fetch Zotero's indexed fulltext for an attachment.

        Uses pyzotero's `fulltext_item(itemKey)` which returns a dict
        with at minimum `content` (the extracted text) plus progress
        info (indexedPages/totalPages for PDFs, indexedChars/totalChars
        for text documents). We only need `content`.

        Returns:
            Extracted text if Zotero has indexed this attachment.
            None if Zotero hasn't indexed it yet (404 / missing content).
            None on any other error — callers should fall through to
            local PDF extraction rather than abort.

        Notes:
          - `attachment_key` must be the ATTACHMENT's itemKey, not the
            parent paper's key. Get it from ZoteroAttachment.key.
          - Works in both web and live modes (pyzotero routes the same
            endpoint). Zotero's indexer runs async on attachment import;
            freshly added PDFs may miss until the daemon catches up.
          - Does not retry; caller is expected to handle retries at the
            batch level if the fulltext importer runs against many
            papers.
        """
        try:
            result = self._z.fulltext_item(attachment_key)
        except Exception as e:
            # 404 when not indexed; also catches network / auth errors.
            # Log at debug — we'll fall through to local extraction and
            # the user gets a clearer signal from the aggregate report.
            log.debug(
                "zotero fulltext_item(%s) unavailable: %s",
                attachment_key, e,
            )
            return None
        if not isinstance(result, dict):
            return None
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return content

    # ------------------------------------------------------------------
    # Item fetch
    # ------------------------------------------------------------------

    def get_paper(self, key: str) -> ZoteroItem:
        """Fetch a paper-like item with its child notes AND attachments attached.

        We call `children()` once and split the result into (notes,
        attachments) — two passes over the same list is fine, and we
        save a network round-trip (important in web mode).
        """
        raw = self._z.item(key)
        data = raw["data"]
        if data.get("itemType") == "note":
            raise ValueError(f"Item {key} is a note, not a paper")

        notes, attachments = self._fetch_children(key)
        return self._build_item(raw, notes=notes, attachments=attachments)

    def get_standalone_note(self, key: str) -> ZoteroItem:
        """Fetch a standalone note as a ZoteroItem.

        The note's own HTML is wrapped as a single-element list in .notes
        so the md builder has a uniform interface. The parent note has
        kind=note, no authors, etc.
        """
        raw = self._z.item(key)
        data = raw["data"]
        if data.get("itemType") != "note":
            raise ValueError(f"Item {key} is not a note")
        if data.get("parentItem"):
            raise ValueError(f"Item {key} is a child note, not standalone")

        self_note = ZoteroNote(
            key=raw["key"],
            version=raw.get("version", 0),
            parent_key=None,
            html=data.get("note", ""),
            date_added=data.get("dateAdded", ""),
            date_modified=data.get("dateModified", ""),
            tags=[t.get("tag", "") for t in data.get("tags", [])],
        )
        # Standalone notes have no attachments.
        return self._build_item(raw, notes=[self_note], attachments=[])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _iter_everything(self, query_gen):
        """Consume pyzotero's paginated result fully.

        pyzotero.everything() handles pagination; we just delegate.
        """
        return self._z.everything(query_gen)

    def _fetch_children(
        self, parent_key: str
    ) -> tuple[list[ZoteroNote], list[ZoteroAttachment]]:
        """Fetch all children of a paper in one request; split by type.

        Returns (notes, attachments). Attachments are filtered to PDFs
        only — other contentTypes (text/html snapshots, images, etc.)
        are dropped, because Phase 1 only handles PDFs.

        Failures in the children fetch (network, permissions, etc.) are
        swallowed here: we return empty lists and let the caller log.
        The alternative — propagating — would abort the whole import
        over a transient network blip.
        """
        notes: list[ZoteroNote] = []
        attachments: list[ZoteroAttachment] = []

        try:
            children = self._z.children(parent_key)
        except Exception:
            return notes, attachments

        for child in children:
            data = child.get("data", {})
            itype = data.get("itemType")

            if itype == "note":
                notes.append(
                    ZoteroNote(
                        key=child["key"],
                        version=child.get("version", 0),
                        parent_key=parent_key,
                        html=data.get("note", ""),
                        date_added=data.get("dateAdded", ""),
                        date_modified=data.get("dateModified", ""),
                        tags=[t.get("tag", "") for t in data.get("tags", [])],
                    )
                )
            elif itype == "attachment":
                # Filter: only PDFs with a filename (= stored locally,
                # not linked/URL attachments).
                ctype = data.get("contentType", "")
                if ctype != "application/pdf":
                    continue
                fname = data.get("filename", "")
                if not fname:
                    # Attachment without a filename is typically a
                    # "linked URL" — no bytes on disk. Skip silently.
                    continue
                attachments.append(
                    ZoteroAttachment(
                        key=child["key"],
                        version=child.get("version", 0),
                        parent_key=parent_key,
                        filename=fname,
                        content_type=ctype,
                        date_added=data.get("dateAdded", ""),
                        date_modified=data.get("dateModified", ""),
                    )
                )
            # Ignore other child types (rare: e.g. child items that
            # aren't notes or attachments — shouldn't happen in
            # practice).
        return notes, attachments

    def _build_item(
        self,
        raw: dict[str, Any],
        notes: list[ZoteroNote],
        attachments: list[ZoteroAttachment],
    ) -> ZoteroItem:
        data = raw["data"]
        return ZoteroItem(
            key=raw["key"],
            version=raw.get("version", 0),
            item_type=data.get("itemType", ""),
            title=data.get("title", ""),
            authors=_extract_authors(data),
            year=_extract_year(data.get("date", "")),
            date=data.get("date", ""),
            publication=(
                data.get("publicationTitle")
                or data.get("bookTitle")
                or data.get("proceedingsTitle")
                or data.get("publisher")
                or ""
            ),
            doi=data.get("DOI", ""),
            url=data.get("url", ""),
            abstract=data.get("abstractNote", ""),
            citation_key=_extract_citation_key(data.get("extra", "")),
            tags=[t.get("tag", "") for t in data.get("tags", [])],
            collections=[
                self._resolve_collection_name(k)
                for k in data.get("collections", [])
            ],
            date_added=data.get("dateAdded", ""),
            date_modified=data.get("dateModified", ""),
            notes=notes,
            attachments=attachments,
        )

    def _resolve_collection_name(self, key: str) -> str:
        if key in self._collection_name_cache:
            return self._collection_name_cache[key]
        try:
            coll = self._z.collection(key)
            name = coll["data"]["name"]
        except Exception:
            name = key  # fallback: use the key itself
        self._collection_name_cache[key] = name
        return name


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------

_YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")
_CITEKEY_RE = re.compile(r"(?im)^Citation Key:\s*(\S+)\s*$")


def _extract_authors(data: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for c in data.get("creators", []):
        if c.get("creatorType") != "author":
            continue
        if "name" in c and c["name"]:
            authors.append(c["name"])
        else:
            last = c.get("lastName", "").strip()
            first = c.get("firstName", "").strip()
            if last and first:
                authors.append(f"{last}, {first}")
            elif last:
                authors.append(last)
            elif first:
                authors.append(first)
    return authors


def _extract_year(date_str: str) -> int | None:
    m = _YEAR_RE.search(date_str)
    return int(m.group()) if m else None


def _extract_citation_key(extra: str) -> str:
    m = _CITEKEY_RE.search(extra)
    return m.group(1) if m else ""
