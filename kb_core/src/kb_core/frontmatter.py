"""Lightweight YAML-frontmatter list extraction.

Two packages need to parse `zotero_attachment_keys`, `authors`,
`kb_tags` and similar list-of-string keys out of paper md
frontmatter without pulling in a full YAML parser: kb-importer
(to find PDFs for re-summarize) and kb-write (to build the
pool for `re-read --source storage`).

Previously each had its own regex, with different bugs:

- kb_importer/resummarize_adapter._extract_frontmatter_list
  only matched block items indented 2 spaces (`  - X`), but
  PyYAML's default dump emits `- X` (0 indent). → no PDF found
  for any real paper.
- kb_write/ops/re_read_sources._FM_ATTACHMENT_FLOW_RE matched
  only the flow form `key: [a, b]`, but real mds are all block.
  → 0 hits on --source storage for any non-trivial library.

v27 consolidates both into one well-tested parser here.
"""
from __future__ import annotations


def extract_list(fm_text: str, key: str) -> list[str]:
    """Parse a YAML list-of-strings scalar from a frontmatter block.

    Accepts both forms PyYAML can produce:

      Flow form:   `key: [a, b, c]`
      Block form:  `key:\\n- a\\n- b\\n`       (0-indent, PyYAML default)
                   `key:\\n  - a\\n  - b\\n`   (2-indent, also legal)

    The input `fm_text` should be the CONTENT of the frontmatter
    block — i.e. the text between the opening `---\\n` and
    closing `\\n---`, NOT including those markers. Callers that
    have a full md should slice first:

        if md.startswith("---\\n"):
            end = md.find("\\n---\\n", 4)
            if end > 0:
                fm = md[4:end]
                items = extract_list(fm, "authors")

    Returns an empty list when the key is missing, mis-typed
    (scalar instead of list), or the block is malformed. Never
    raises — caller code should treat "empty list" as "not set
    or unparseable" and degrade gracefully.

    Quote stripping: we remove at most one layer of surrounding
    ASCII single / double quotes from each item. YAML-escape
    sequences inside quoted scalars are NOT interpreted; callers
    that need `\\n` escapes or Unicode escapes inside values
    should use a real YAML parser. In practice all keys this is
    called for hold simple identifiers (Zotero keys, tag names,
    author strings) and quote-stripping suffices.
    """
    # Flow form: key: [a, b, c] on a single line.
    for line in fm_text.splitlines():
        s = line.strip()
        if s.startswith(f"{key}:"):
            rest = s.split(":", 1)[1].strip()
            if rest.startswith("[") and rest.endswith("]"):
                return [
                    x.strip().strip('"').strip("'")
                    for x in rest[1:-1].split(",") if x.strip()
                ]
            # First occurrence of `key:` wasn't flow-form; don't
            # fall through to yet another scan — break and try the
            # block-form pass below.
            break

    # Block form. Find the `key:` line (with empty rhs), then
    # consume following lines that look like list items until
    # indent returns to the key's level or lower.
    lines = fm_text.splitlines()
    out: list[str] = []
    capturing = False
    key_indent = 0
    for line in lines:
        if not capturing:
            stripped = line.lstrip()
            if (
                stripped.startswith(f"{key}:")
                and stripped.rstrip().endswith(":")
            ):
                capturing = True
                key_indent = len(line) - len(stripped)
                continue
        else:
            if not line.strip():
                # Blank line mid-list is tolerated.
                continue
            this_indent = len(line) - len(line.lstrip())
            stripped = line.lstrip()
            if stripped.startswith("- "):
                # Item — accept. We don't enforce a specific indent
                # because PyYAML's default (0-indent relative to
                # the key) and the alternative (2-indent) both
                # satisfy "same-or-deeper than the key column".
                if this_indent >= key_indent:
                    out.append(
                        stripped[2:].strip().strip('"').strip("'")
                    )
                    continue
            # A non-item line at the key's column or shallower means
            # we've left the list's scope — next top-level key.
            if this_indent <= key_indent:
                break
            # Otherwise: deeper nested content (multi-line scalar
            # under an item, etc.). Ignore safely — our callers
            # only ever need flat list-of-strings.
    return out
