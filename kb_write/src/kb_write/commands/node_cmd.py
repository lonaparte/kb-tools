"""`kb-write thought` and `kb-write topic` — node md ops.

Both follow the same create / update shape, so they live together.
"""
from __future__ import annotations

from pathlib import Path

from ..ops import thought as thought_ops
from ..ops import topic as topic_ops
from ._shared import _read_body, _emit_result


# ---------- thought ----------
def register_thought(sub) -> None:
    t = sub.add_parser("thought", help="Thought md ops.")
    ts = t.add_subparsers(dest="thought_cmd", required=True)

    c = ts.add_parser("create", help="Create a new thought.")
    c.add_argument("--title", required=True)
    c.add_argument("--slug", help="Optional; auto-generated from title + today if absent.")
    c.add_argument("--body-file", type=Path, required=True,
                   help="Path to a file containing the md body (or '-' for stdin).")
    c.add_argument("--ref", action="append", default=[], dest="refs",
                   help="kb_refs entry (repeatable).")
    c.add_argument("--tag", action="append", default=[], dest="tags",
                   help="kb_tags entry (repeatable).")
    c.set_defaults(func=_cmd_thought_create)

    u = ts.add_parser("update", help="Update an existing thought.")
    u.add_argument("target",
                   help="thoughts/SLUG or SLUG.")
    u.add_argument("--expected-mtime", type=float, required=True,
                   help="mtime from your last read (conflict guard).")
    u.add_argument("--body-file", type=Path,
                   help="New body (optional; omit to leave body unchanged).")
    u.add_argument("--title")
    u.add_argument("--ref", action="append", dest="refs")
    u.add_argument("--refs-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.add_argument("--tag", action="append", dest="tags")
    u.add_argument("--tags-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.set_defaults(func=_cmd_thought_update)


def _cmd_thought_create(args, ctx):
    body = _read_body(args.body_file)
    result = thought_ops.create(
        ctx, title=args.title, body=body,
        slug=args.slug, refs=args.refs, tags=args.tags,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_thought_update(args, ctx):
    body = _read_body(args.body_file) if args.body_file else None
    target = args.target
    if not target.startswith("thoughts/") and "/" not in target:
        target = f"thoughts/{target}"
    result = thought_ops.update(
        ctx, target, expected_mtime=args.expected_mtime,
        body=body, title=args.title,
        refs=args.refs, refs_mode=args.refs_mode,
        tags=args.tags, tags_mode=args.tags_mode,
    )
    _emit_result(args, ctx, result)
    return 0


# ---------- topic ----------
def register_topic(sub) -> None:
    t = sub.add_parser("topic", help="Topic md ops.")
    ts = t.add_subparsers(dest="topic_cmd", required=True)

    c = ts.add_parser("create", help="Create a new topic.")
    c.add_argument("--slug", required=True)
    c.add_argument("--title", required=True)
    c.add_argument("--body-file", type=Path, required=True)
    c.add_argument("--ref", action="append", default=[], dest="refs")
    c.add_argument("--tag", action="append", default=[], dest="tags")
    c.set_defaults(func=_cmd_topic_create)

    u = ts.add_parser("update", help="Update an existing topic.")
    u.add_argument("target")
    u.add_argument("--expected-mtime", type=float, required=True)
    u.add_argument("--body-file", type=Path)
    u.add_argument("--title")
    u.add_argument("--ref", action="append", dest="refs")
    u.add_argument("--refs-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.add_argument("--tag", action="append", dest="tags")
    u.add_argument("--tags-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.set_defaults(func=_cmd_topic_update)


def _cmd_topic_create(args, ctx):
    body = _read_body(args.body_file)
    result = topic_ops.create(
        ctx, slug=args.slug, title=args.title, body=body,
        refs=args.refs, tags=args.tags,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_topic_update(args, ctx):
    body = _read_body(args.body_file) if args.body_file else None
    target = args.target
    if not target.startswith("topics/") and "/" not in target:
        target = f"topics/{target}"
    result = topic_ops.update(
        ctx, target, expected_mtime=args.expected_mtime,
        body=body, title=args.title,
        refs=args.refs, refs_mode=args.refs_mode,
        tags=args.tags, tags_mode=args.tags_mode,
    )
    _emit_result(args, ctx, result)
    return 0
