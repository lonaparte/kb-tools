"""`kb-write pref` — agent preferences in `.agent-prefs/`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..ops import preference as pref_ops
from ..rules import RuleViolation
from ._shared import _read_body, _emit_result, _resolve_kb_root


def register(sub) -> None:
    p = sub.add_parser("pref", help="Agent preferences (.agent-prefs/).")
    ps = p.add_subparsers(dest="pref_cmd", required=True)

    add = ps.add_parser("add", help="Create a new preference file.")
    add.add_argument("--slug", required=True)
    add.add_argument("--body-file", type=Path, required=True)
    add.add_argument("--scope", default="global",
                     help="Scope tag (global/writing/research/ai-summary/...).")
    add.add_argument("--priority", type=int, default=50)
    add.add_argument("--title")
    add.set_defaults(func=_cmd_pref_add)

    up = ps.add_parser("update", help="Update an existing preference.")
    up.add_argument("slug")
    up.add_argument("--expected-mtime", type=float, required=True)
    up.add_argument("--body-file", type=Path)
    up.add_argument("--scope")
    up.add_argument("--priority", type=int)
    up.add_argument("--title")
    up.set_defaults(func=_cmd_pref_update)

    ls = ps.add_parser("list", help="List all preferences.")
    ls.set_defaults(func=_cmd_pref_list)

    sh = ps.add_parser("show", help="Print a preference file's content.")
    sh.add_argument("slug", nargs="?",
                    help="If omitted, dumps all prefs formatted for agent.")
    sh.add_argument("--scope", help="Filter by scope when dumping all.")
    sh.set_defaults(func=_cmd_pref_show)


def _cmd_pref_add(args, ctx):
    body = _read_body(args.body_file)
    result = pref_ops.create(
        ctx, slug=args.slug, body=body,
        scope=args.scope, priority=args.priority,
        title=args.title,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_pref_update(args, ctx):
    body = _read_body(args.body_file) if args.body_file else None
    result = pref_ops.update(
        ctx, slug=args.slug, expected_mtime=args.expected_mtime,
        body=body, scope=args.scope, priority=args.priority,
        title=args.title,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_pref_list(args, ctx):
    kb_root = _resolve_kb_root(args)
    prefs = pref_ops.list_all(kb_root)
    if args.json:
        print(json.dumps(prefs, indent=2, default=str))
        return 0
    if not prefs:
        print("(no preferences yet)")
        return 0
    for p in prefs:
        print(f"  [{p['scope']}, pri={p['priority']}, updated={p['last_updated']}] "
              f"{p['slug']}: {p['title']}")
        print(f"      path: {p['path']}, mtime={p['mtime']:.9f}")
    return 0


def _cmd_pref_show(args, ctx):
    kb_root = _resolve_kb_root(args)
    if args.slug:
        # Validate slug the same way create/update/delete do, so a
        # caller passing "../something" can't read outside the
        # .agent-prefs/ dir. Previously this CLI skipped validation —
        # letting `kb-write pref show ../../../etc/passwd` actually
        # try to read /etc/passwd.md. _validate_pref_slug rejects
        # the `/` and the `..` shapes both.
        from ..ops.preference import _validate_pref_slug
        try:
            _validate_pref_slug(args.slug)
        except RuleViolation as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

        md_path = (kb_root / ".agent-prefs" / f"{args.slug}.md").resolve()
        # Defense-in-depth: even after slug validation, hard-check
        # that the resolved path stays inside .agent-prefs/.
        prefs_root = (kb_root / ".agent-prefs").resolve()
        try:
            md_path.relative_to(prefs_root)
        except ValueError:
            print(
                f"error: resolved path {md_path} escapes "
                f"{prefs_root}; refusing to read.",
                file=sys.stderr,
            )
            return 2

        if not md_path.exists():
            print(f"error: no such preference: {args.slug}", file=sys.stderr)
            return 1
        print(md_path.read_text(encoding="utf-8"))
        print(f"\n<!-- mtime: {md_path.stat().st_mtime:.9f} -->")
    else:
        print(pref_ops.read_all_for_agent(kb_root, scope=args.scope or "all"))
    return 0
