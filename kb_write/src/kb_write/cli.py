"""kb-write command-line entry point.

v0.28.0: per-subcommand argparse wiring lives under `commands/`.
This module stays slim: it builds the top-level parser, delegates
subcommand registration to `commands.register_all`, and runs the
main() dispatch loop (context building, error translation to exit
codes).

Subcommands registered:
    init                      — scaffold a fresh KB
    thought create/update     — thought md ops
    topic create/update       — topic md ops
    pref add/update/list/show — agent preferences
    ai-zone append/show       — AI-zone entries
    tag add/remove            — kb_tags list ops
    ref add/remove            — kb_refs list ops
    delete                    — delete thought/topic/pref (with --yes)
    log                       — tail kb-write audit log
    rules                     — print AGENT-WRITE-RULES.md
    doctor                    — scan KB for violations (--fix for high-conf)
    re-summarize              — re-run summariser on one paper
    re-read                   — batch re-summarize with selector strategies
    migrate-legacy-chapters   — v25 chapter layout → v26
    migrate-slugs             — rename slugs to canonical lowercase-kebab

Deliberately uses argparse rather than click — no extra dep for
local agents to install.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .atomic import WriteConflictError, WriteExistsError
from .commands import register_all
from .commands._shared import _build_context
from .paths import PathError
from .rules import RuleViolation
from .safety import _check_unsafe_flags, _UNSAFE_FLAGS_OPT_IN_ENV  # noqa: F401
from .zones import ZoneError


def _parser() -> argparse.ArgumentParser:
    from . import __version__
    p = argparse.ArgumentParser(
        prog="kb-write",
        description="Write layer for the ee-kb knowledge base. "
                    "See AGENT-WRITE-RULES.md for invariants.",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s v{__version__}")
    p.add_argument("--kb-root", type=Path,
                   help="KB repo root (defaults to $KB_ROOT).")
    p.add_argument("--no-git-commit", action="store_true",
                   help="Skip git auto-commit for this operation.")
    p.add_argument("--no-reindex", action="store_true",
                   help="Skip `kb-mcp index` after writing.")
    p.add_argument("--no-lock", action="store_true",
                   help="Skip write lock (unsafe; for debugging).")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate everything but don't write.")
    p.add_argument("--commit-message", "-m",
                   help="Extra body for the git commit message.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON for stdout.")
    p.add_argument("--absolute", action="store_true",
                   help="Print absolute paths instead of kb-relative "
                        "paths in human output. Useful when you need "
                        "to pipe into `vim` / `cd` / `ls` etc. JSON "
                        "output always uses kb-relative paths "
                        "regardless of this flag.")

    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    register_all(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _check_unsafe_flags(args)

    # Commands that don't need a WriteContext because they don't write
    # or because they build their own narrowly-scoped ctx.
    # - init: builds ctx minimally internally (it sidesteps lock on
    #         a fresh KB).
    # - rules: pure read.
    # - doctor: builds its own narrow ctx (no lock, no git).
    # - pref list/show: pure read.
    # - ai-zone show: pure read.
    no_ctx_cmds = {"init", "rules", "doctor", "log"}
    read_only_subcmds = {
        ("pref", "list"),
        ("pref", "show"),
        ("ai-zone", "show"),
    }
    ctx = None
    if args.command in no_ctx_cmds:
        ctx = None
    else:
        # Grab whatever sub-command attribute is present (varies by parent).
        sub = (
            getattr(args, "pref_cmd", None)
            or getattr(args, "ai_zone_cmd", None)
            or getattr(args, "thought_cmd", None)
            or getattr(args, "topic_cmd", None)
            or getattr(args, "tag_cmd", None)
            or getattr(args, "ref_cmd", None)
        )
        if (args.command, sub) in read_only_subcmds:
            ctx = None
        else:
            ctx = _build_context(args)

    try:
        return args.func(args, ctx)
    except RuleViolation as e:
        print(f"rule violation: {e}", file=sys.stderr)
        return 3
    except WriteConflictError as e:
        print(f"write conflict: {e}", file=sys.stderr)
        return 4
    except WriteExistsError as e:
        print(f"already exists: {e}", file=sys.stderr)
        return 5
    except ZoneError as e:
        print(f"AI-zone error: {e}", file=sys.stderr)
        return 6
    except PathError as e:
        print(f"path error: {e}", file=sys.stderr)
        return 7
    except FileNotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"unexpected error: {e!r}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
