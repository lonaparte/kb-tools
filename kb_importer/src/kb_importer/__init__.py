"""kb-importer: Zotero library → KB markdown files.

Two source modes (selected via Config):
- "live" (default): Zotero 7 local HTTP API at localhost:23119.
  Requires Zotero to be running on the same host.
- "web": Zotero cloud API (api.zotero.org). Needs a library_id and
  API key. Network required, but no local Zotero needed.

Both modes find PDFs in the same local `zotero_storage_dir`.

TODO: A future "sqlite" mode could read zotero.sqlite directly for
fully offline operation without network.

See the specification document for the full design.
"""
__version__ = "0.29.8"
