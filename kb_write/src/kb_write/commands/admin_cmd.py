"""`kb-write delete / log / rules / doctor` — admin & inspection."""
from __future__ import annotations

import json
import sys
from importlib import resources

from ..config import WriteContext
from ..ops import delete as delete_ops
from ..ops import doctor as doctor_ops
from ._shared import (
    _positive_int, _fmt_path, _resolve_kb_root,
)


# ---------- delete ----------
def register_delete(sub) -> None:
    p = sub.add_parser(
        "delete", help="Delete a thought/topic/preference (requires --yes).",
    )
    p.add_argument("target",
                   help="E.g. thoughts/2026-04-22-x, topics/X, "
                        ".agent-prefs/writing-style")
    p.add_argument("--yes", action="store_true",
                   help="Required confirmation flag. Without it, delete "
                        "refuses to proceed.")
    p.set_defaults(func=_cmd_delete)


def _cmd_delete(args, ctx):
    if not args.yes:
        print(
            "refusing to delete without --yes. "
            "This guard prevents accidental deletion.",
            file=sys.stderr,
        )
        return 2
    result = delete_ops.delete(ctx, args.target, confirm=True)
    if args.json:
        # JSON always kb-relative: downstream scripts shouldn't
        # depend on the caller's home layout.
        print(json.dumps({
            "node_type": result.address.node_type,
            "key": result.address.key,
            "deleted": result.address.md_rel_path,
            "git_sha": result.git_sha,
            "reindexed": result.reindexed,
        }, indent=2))
    else:
        print(f"  deleted: {result.address.node_type}/{result.address.key}")
        print(f"    path: {_fmt_path(result.md_path, ctx.kb_root, absolute=args.absolute)}")
        if result.git_sha:
            print(f"    git: {result.git_sha[:12]}")
    return 0


# ---------- log ----------
def register_log(sub) -> None:
    p = sub.add_parser(
        "log",
        help="Show recent kb-write operations from the audit log.",
    )
    p.add_argument("-n", type=_positive_int, default=20,
                   help="Number of entries to show (default 20).")
    p.add_argument("--op", help="Filter by op name (e.g. create_thought).")
    p.add_argument("--actor", help="Filter by actor (cli/mcp/python).")
    p.set_defaults(func=_cmd_log)


def _cmd_log(args, ctx):
    kb_root = _resolve_kb_root(args)
    from ..audit import tail
    entries = tail(kb_root, n=max(args.n, 1) * 4)  # over-fetch for filter
    if args.op:
        entries = [e for e in entries if e.get("op") == args.op]
    if args.actor:
        entries = [e for e in entries if e.get("actor") == args.actor]
    entries = entries[-args.n:]
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("(no audit entries)")
        return 0
    for e in entries:
        ts = e.get("ts", "?")[:19]
        actor = e.get("actor", "?")
        op = e.get("op", "?")
        target = e.get("target", "?")
        sha = e.get("git_sha", "")
        sha_str = f" [git {sha[:8]}]" if sha else ""
        print(f"{ts}  {actor:>6}  {op:<20}  {target}{sha_str}")
    return 0


# ---------- rules ----------
def register_rules(sub) -> None:
    p = sub.add_parser("rules", help="Print AGENT-WRITE-RULES.md.")
    p.set_defaults(func=_cmd_rules)


def _cmd_rules(args, ctx):
    pkg = resources.files("kb_write")
    print((pkg / "AGENT-WRITE-RULES.md").read_text(encoding="utf-8"))
    return 0


# ---------- doctor ----------
def register_doctor(sub) -> None:
    p = sub.add_parser("doctor", help="Scan KB for rule violations.")
    p.add_argument("--fix", action="store_true",
                   help="Auto-repair safely-fixable issues.")
    p.set_defaults(func=_cmd_doctor)


def _cmd_doctor(args, ctx):
    # Doctor doesn't go through the normal ctx lifecycle (it reads
    # lots of files and may write scaffolds); it uses its own small
    # context. Build one here without lock (to allow concurrent reads
    # of same KB) and without git-commit (repairs are manual-review).
    kb_root = _resolve_kb_root(args)
    ctx = WriteContext(
        kb_root=kb_root,
        git_commit=False,
        reindex=False,
        lock=False,
    )
    report = doctor_ops.doctor(ctx, fix=args.fix)
    if args.json:
        print(json.dumps({
            "scanned": report.scanned,
            "fixed": report.fixed,
            "findings": [
                {"severity": f.severity, "category": f.category,
                 "path": f.path, "message": f.message,
                 "auto_fixable": f.auto_fixable}
                for f in report.findings
            ],
        }, indent=2))
    else:
        print(doctor_ops.format_report(report))
    return 1 if report.has_errors() else 0
