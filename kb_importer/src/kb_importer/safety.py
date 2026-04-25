"""Agent-safety gate for kb-importer.

1.4.3: extracted from cli.py so unit tests can import the gate
without dragging the full Zotero / OpenAI / file-pipeline imports
into the test process. Mirrors kb_write.safety; both gates read the
same env var so a single shell-level opt-in covers both tools.
"""
from __future__ import annotations

import argparse
import os
import sys


# Same string as kb_write.safety._UNSAFE_FLAGS_OPT_IN_ENV — must
# stay in sync. test_security_wave4.test_env_var_name_matches_across_tools
# pins this.
_UNSAFE_FLAGS_OPT_IN_ENV = "KB_WRITE_ALLOW_UNSAFE_FLAGS"


def _check_unsafe_flags(args: argparse.Namespace) -> None:
    """Reject runs that pass --no-git-commit without an explicit
    opt-in. SystemExit(2) on rejection."""
    if not getattr(args, "no_git_commit", False):
        return
    if os.environ.get(_UNSAFE_FLAGS_OPT_IN_ENV) == "1":
        return
    print(
        "error: refusing to use unsafe flag --no-git-commit without "
        "explicit opt-in. This flag bypasses kb-importer's auto-commit "
        "and exists only for human-driven debugging.\n"
        f"  To opt in for this shell session, set:\n"
        f"    export {_UNSAFE_FLAGS_OPT_IN_ENV}=1",
        file=sys.stderr,
    )
    sys.exit(2)
