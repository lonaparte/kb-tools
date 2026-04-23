"""Frontmatter manipulation with ownership-aware semantics.

Three operations, each respecting AGENT-WRITE-RULES §3:

- `read_frontmatter(path)` → (dict, body, mtime)
- `write_frontmatter(path, fm, body, expected_mtime)`: atomic
  rewrite of the whole md with new frontmatter + body.
- `merge_kb_fields(fm, updates)`: in-place update of kb_tags /
  kb_refs using merge-append-dedupe, and kb_* scalars via simple
  overwrite. Refuses to touch zotero_* and fulltext_* fields.

The distinction matters: we never want an agent's `add_kb_tag` call
to accidentally wipe zotero_tags or reset fulltext_processed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter

from .atomic import atomic_write
from .rules import PROTECTED_FIELDS, PROTECTED_PREFIXES

# kb_* list-type fields use merge-append-dedupe semantics.
KB_LIST_FIELDS = frozenset({"kb_tags", "kb_refs"})


class FrontmatterError(Exception):
    pass


def read_md(path: Path) -> tuple[dict, str, float]:
    """Load an md file, return (frontmatter_dict, body, mtime).

    mtime is captured at read time for the caller to pass back as
    expected_mtime in a later write — realizing the mtime guard
    protocol.
    """
    path = Path(path)
    if not path.exists():
        raise FrontmatterError(f"{path} does not exist")
    post = frontmatter.load(str(path))
    mtime = path.stat().st_mtime
    return dict(post.metadata), post.content, mtime


def write_md(
    path: Path,
    fm: dict,
    body: str,
    *,
    expected_mtime: float | None,
    create_only: bool = False,
) -> float:
    """Compose fm + body into an md file and atomically write it.

    Returns the new mtime after the write.
    """
    post = frontmatter.Post(body, **fm)
    text = frontmatter.dumps(post)
    # frontmatter.dumps returns text without a trailing newline in
    # some cases — normalize for nicer git diffs.
    if not text.endswith("\n"):
        text += "\n"
    atomic_write(
        Path(path), text,
        expected_mtime=expected_mtime,
        create_only=create_only,
    )
    return Path(path).stat().st_mtime


def merge_kb_fields(
    existing: dict, updates: dict,
) -> dict:
    """Return a new dict: `existing` with `updates` applied, but only
    for agent-writable fields.

    Protected fields in `updates` (zotero_*, fulltext_*, kind, title,
    etc.) are silently ignored — NOT an error. This lets agents pass
    the whole current frontmatter back with a few changes, without
    having to surgically strip protected keys.

    List fields (kb_tags, kb_refs): union, dedupe, preserve insertion
    order of existing items first then new items.

    Other kb_* and unknown fields: overwrite (updates wins).
    """
    result = dict(existing)
    for key, new_value in updates.items():
        if _is_protected(key):
            continue
        if key in KB_LIST_FIELDS:
            result[key] = _merge_list(
                existing.get(key) or [], new_value or []
            )
        else:
            result[key] = new_value
    return result


def remove_from_kb_list(
    fm: dict, field: str, value: Any,
) -> dict:
    """Remove `value` from the list at `fm[field]`. No-op if
    value isn't present. `field` must be an agent-writable list
    field (kb_tags / kb_refs)."""
    if field not in KB_LIST_FIELDS:
        raise FrontmatterError(
            f"{field!r} is not a supported list field; expected one of "
            f"{sorted(KB_LIST_FIELDS)}."
        )
    result = dict(fm)
    current = result.get(field) or []
    if not isinstance(current, list):
        return result
    result[field] = [x for x in current if x != value]
    return result


def _is_protected(key: str) -> bool:
    if key in PROTECTED_FIELDS:
        return True
    for prefix in PROTECTED_PREFIXES:
        if key.startswith(prefix):
            return True
    return False


def _merge_list(existing: list, new: list) -> list:
    """Append items from `new` that aren't already in `existing`,
    preserving order. Non-string items in either list pass through
    unchanged (defensive; shouldn't happen under normal use)."""
    if not isinstance(existing, list):
        existing = []
    if not isinstance(new, list):
        return existing
    seen: set = set()
    out: list = []
    for item in list(existing) + list(new):
        # Use repr for set membership to handle unhashable edge cases
        # (unlikely: kb_tags are strings).
        try:
            key = item
            if key in seen:
                continue
            seen.add(key)
        except TypeError:
            # Unhashable — rare; include regardless.
            pass
        out.append(item)
    return out
