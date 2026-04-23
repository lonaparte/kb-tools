"""`kb-write init` — scaffold a KB with discovery files."""
from __future__ import annotations

import json

from ..config import WriteContext
from ..ops import init as init_ops
from ._shared import _resolve_kb_root


def register(sub) -> None:
    p = sub.add_parser("init", help="Scaffold a KB with discovery files.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite ALL scaffold files (destructive).")
    p.add_argument("--refresh", action="store_true",
                   help="Re-render prompt files (CLAUDE.md/AGENTS.md/README.md) "
                        "from the latest fragments, preserving any user content "
                        "appended AFTER the generated block.")
    p.set_defaults(func=_cmd_init)


def _cmd_init(args, ctx: WriteContext | None):
    kb_root = _resolve_kb_root(args, allow_missing=True)
    report = init_ops.init_kb(
        kb_root, force=args.force, refresh=args.refresh,
    )
    if args.json:
        print(json.dumps({
            "kb_root": str(kb_root),
            "created": report.created,
            "refreshed": report.refreshed,
            "skipped_existing": report.skipped_existing,
            "overwritten": report.overwritten,
        }, indent=2))
    else:
        print(f"kb-write init: {kb_root}")
        for f in report.created:
            print(f"  created    {f}")
        for f in report.refreshed:
            print(f"  refreshed  {f}  (generated block updated, user suffix preserved)")
        for f in report.overwritten:
            print(f"  overwrote  {f}")
        for f in report.skipped_existing:
            print(f"  skipped    {f} (already exists; use --refresh or --force)")
        if not (report.created or report.refreshed or report.overwritten):
            print("  nothing to do.")
    return 0
