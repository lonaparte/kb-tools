"""Output formatting helpers shared across CLI and MCP surfaces.

Deliberately minimal — this module exists to stop the same
"render a path / render an error / order these JSON fields"
logic from drifting across 3+ places (kb-write CLI, kb-mcp tool
replies, kb-mcp report). No heavy framework: pure functions that
string-return, callers decide when to use them.

What's NOT here (and shouldn't be):
- Anything that reads or writes files
- Any business logic (classification, selection, aggregation)
- Anything depending on a specific Result / Report dataclass —
  formatters take primitives (strings, dicts) so they can be
  re-used across shape-specific callers

If you find yourself adding `from kb_write.ops.X import Result`
here, stop: move the helper you actually wanted into the caller
and pass the primitives in.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def render_path(
    path: str | Path, kb_root: str | Path | None = None, *,
    absolute: bool = False,
) -> str:
    """Render a filesystem path for display.

    Default: kb-relative POSIX (e.g. "papers/ABCD1234.md") — safe
    to paste into chat / issues / agent prompts without leaking
    operator home directory.

    `absolute=True`: full path. Useful for piping into `vim` / `cd`
    / `ls` when the user is on their own machine and explicitly
    asked (--absolute flag).

    If kb_root is None or the path doesn't lie under it, falls back
    to str(path). That means "safe" = never raises; worst case we
    emit a longer string than ideal.
    """
    p = Path(path)
    if absolute or kb_root is None:
        return str(p)
    root = Path(kb_root)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return str(p)


def render_error(
    message: str, *,
    prefix: str = "error",
    code: str | None = None,
) -> str:
    """Render an error string with a consistent "<prefix>: <msg>"
    shape. If a code is provided, appends "[code=X]" so parsers
    that want structured info have a stable tag.

    Convention used across kb-write CLI and kb-mcp tool replies:
      - prefix "error"   → one-liner human-facing error
      - prefix "✗"        → CLI per-item failure in a batch
      - prefix "warning" → non-fatal heads-up
    """
    if code:
        return f"{prefix}: {message} [code={code}]"
    return f"{prefix}: {message}"


# Preferred field order for JSON payloads about a single written
# node. CLI uses this via dict.dump; MCP tools can too. Keeping the
# order stable makes diffs between runs (and between CLI and MCP
# outputs) compare cleanly.
WRITE_RESULT_FIELD_ORDER = (
    "node_type",
    "key",
    "md_path",         # always kb-relative in JSON (see render_path)
    "md_rel_path",     # duplicate alias for explicit callers
    "mtime",
    "git_sha",
    "reindexed",
    "dry_run",
    "diff",
    "preview",
)


def render_json(payload: dict, field_order: tuple = ()) -> str:
    """JSON-dump a payload with a stable field order.

    Fields listed in `field_order` come first in the order given;
    any fields present in payload but not in field_order come after,
    sorted alphabetically. Unknown fields in `field_order` are
    silently skipped (so a new payload key isn't a regression).
    """
    ordered: dict[str, Any] = {}
    for key in field_order:
        if key in payload:
            ordered[key] = payload[key]
    remaining = sorted(set(payload) - set(field_order))
    for key in remaining:
        ordered[key] = payload[key]
    return json.dumps(ordered, indent=2, ensure_ascii=False)
