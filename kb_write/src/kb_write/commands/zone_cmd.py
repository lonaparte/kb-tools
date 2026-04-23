"""`kb-write ai-zone` — append entries to / show AI-zone content."""
from __future__ import annotations

import json
from pathlib import Path

from ..ops import ai_zone as ai_zone_ops
from ._shared import _read_body, _emit_result, _resolve_kb_root


def register(sub) -> None:
    p = sub.add_parser(
        "ai-zone",
        help="Append a dated entry to the AI zone of a paper/note md."
    )
    ps = p.add_subparsers(dest="ai_zone_cmd", required=True)

    # v26: append replaces the old `update` (which did full replace).
    a = ps.add_parser(
        "append",
        help=(
            "Insert a new dated entry at the top of the AI zone. "
            "Older entries are preserved verbatim — append-only. "
            "Each entry gets `### YYYY-MM-DD — <title>` as its heading."
        ),
    )
    a.add_argument(
        "target",
        help="Which md to edit. E.g. papers/ABCD1234, "
             "papers/BOOKKEY-ch03 (a book chapter), or "
             "topics/standalone-note/NOTEKEY.",
    )
    a.add_argument("--expected-mtime", type=float, required=True)
    a.add_argument(
        "--title", required=True,
        help="One-line title for this entry (no newlines).",
    )
    a.add_argument(
        "--body-file", type=Path, required=True,
        help="Path to file with entry body; '-' for stdin.",
    )
    a.add_argument(
        "--date", default=None,
        help="Override entry date (ISO YYYY-MM-DD). Defaults to today.",
    )
    a.set_defaults(func=_cmd_ai_zone_append)

    r = ps.add_parser("show", help="Print current AI-zone body + mtime.")
    r.add_argument("target")
    r.set_defaults(func=_cmd_ai_zone_show)


def _cmd_ai_zone_append(args, ctx):
    from datetime import date as _date
    entry_date = _date.fromisoformat(args.date) if args.date else None
    body = _read_body(args.body_file)
    result = ai_zone_ops.append(
        ctx, args.target,
        expected_mtime=args.expected_mtime,
        title=args.title,
        body=body,
        entry_date=entry_date,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_ai_zone_show(args, ctx):
    kb_root = _resolve_kb_root(args)
    body, mtime = ai_zone_ops.read_zone(kb_root, args.target)
    if args.json:
        print(json.dumps({"body": body, "mtime": mtime}, indent=2))
    else:
        print(body)
        print(f"\n<!-- mtime: {mtime:.9f} -->")
    return 0
