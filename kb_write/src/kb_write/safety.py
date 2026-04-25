"""Agent-safety gates that must run *before* any heavy CLI work.

1.4.3: extracted from cli.py so unit tests can import the gate
function without pulling in the full command tree (which transitively
loads python-frontmatter, sqlite-vec, etc.). The gate is a few
stdlib-only lines; isolating it keeps the test surface narrow.

The opt-in env var name is shared with kb-importer so a single
`export KB_WRITE_ALLOW_UNSAFE_FLAGS=1` covers a debugging session
that hits both tools.
"""
from __future__ import annotations

import argparse
import os
import sys


# Sentinel name reused by kb-importer.cli — keep them identical.
_UNSAFE_FLAGS_OPT_IN_ENV = "KB_WRITE_ALLOW_UNSAFE_FLAGS"


def _check_unsafe_flags(args: argparse.Namespace) -> None:
    """Reject runs that combine an unsafe flag with no explicit
    opt-in. Prints an error and calls sys.exit(2) on rejection.

    Only --no-lock and --no-git-commit are gated. --no-reindex is
    *not* gated — stale-search is recoverable (run `kb-mcp index`)
    whereas data loss / concurrent-write corruption from the other
    two flags is not.
    """
    unsafe = []
    if getattr(args, "no_lock", False):
        unsafe.append("--no-lock")
    if getattr(args, "no_git_commit", False):
        unsafe.append("--no-git-commit")
    if not unsafe:
        return
    if os.environ.get(_UNSAFE_FLAGS_OPT_IN_ENV) == "1":
        return
    print(
        f"error: refusing to use unsafe flag(s) {', '.join(unsafe)} "
        f"without explicit opt-in. These flags disable kb-write's "
        f"data-integrity guarantees and exist only for human-driven "
        f"debugging.\n"
        f"  To opt in for this shell session, set:\n"
        f"    export {_UNSAFE_FLAGS_OPT_IN_ENV}=1\n"
        f"  See `kb-write rules` for the full safety contract.",
        file=sys.stderr,
    )
    sys.exit(2)
