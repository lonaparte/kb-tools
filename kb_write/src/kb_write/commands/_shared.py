"""Shared helpers for the per-subcommand modules.

These were inline in the pre-split cli.py; extracting them lets each
command module stay focused on its own argparse + dispatch code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from kb_core.argtypes import positive_int as _positive_int  # noqa: F401

from ..config import WriteContext, kb_root_from_env


def _fmt_path(path: Path, kb_root: Path, *, absolute: bool) -> str:
    """Thin delegate to kb_core.format.render_path so behaviour is
    shared with other packages."""
    from kb_core.format import render_path
    return render_path(path, kb_root, absolute=absolute)


def _resolve_kb_root(args, *, allow_missing: bool = False) -> Path:
    try:
        root = kb_root_from_env(args.kb_root)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    if not allow_missing and not root.exists():
        print(f"error: kb_root does not exist: {root}", file=sys.stderr)
        sys.exit(2)
    return root


def _build_context(args) -> WriteContext:
    return WriteContext(
        kb_root=_resolve_kb_root(args),
        git_commit=not args.no_git_commit,
        reindex=not args.no_reindex,
        lock=not args.no_lock,
        commit_message=args.commit_message,
        dry_run=args.dry_run,
        # Stamp every CLI-originated write as actor="cli" so the
        # audit log can distinguish CLI use from MCP / Python API.
        actor="cli",
    )


def _read_body(path: Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _emit_result(args, ctx, result):
    if args.json:
        payload = {
            "node_type": result.address.node_type,
            "key": result.address.key,
            # JSON always kb-relative (stable across host layouts).
            "md_path": result.address.md_rel_path,
            "md_rel_path": result.address.md_rel_path,
            "mtime": result.mtime,
            "git_sha": result.git_sha,
            "reindexed": result.reindexed,
            "dry_run": args.dry_run,
        }
        if result.diff:
            payload["diff"] = result.diff
        if result.preview:
            payload["preview"] = result.preview
        print(json.dumps(payload, indent=2))
        return

    rendered_path = _fmt_path(
        result.md_path, ctx.kb_root, absolute=args.absolute,
    )

    if args.dry_run:
        print(f"  [dry-run] {result.address.node_type}/{result.address.key}")
        print(f"    path: {rendered_path}")
        if result.preview:
            print()
            print(result.preview)
        elif result.diff:
            print()
            print(result.diff, end="")
        else:
            print("    (no changes — write would be a no-op)")
        return

    print(f"  ✓ {result.address.node_type}/{result.address.key}")
    print(f"    path:  {rendered_path}")
    print(f"    mtime: {result.mtime:.9f}")
    if result.git_sha:
        print(f"    git:   {result.git_sha[:12]}")
    if result.reindexed:
        print(f"    reindex: ok")
