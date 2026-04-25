"""argparse `type=` helpers shared across CLIs.

Each tool (kb-write, kb-mcp, kb-importer, kb-citations) had an
identical `_positive_int` and (mostly) identical `_nonnegative_int`
defined locally. Centralised here so the message text + bounds stay
in lockstep across the toolchain.

Use:
    from kb_core.argtypes import positive_int, nonnegative_int
    p.add_argument("--limit", type=positive_int, ...)
"""
from __future__ import annotations

import argparse


def positive_int(value: str) -> int:
    """argparse `type=`: accept positive ints (>= 1), reject others."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"must be an integer, got {value!r}"
        )
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {n}")
    return n


def nonnegative_int(value: str) -> int:
    """argparse `type=`: accept zero and positive ints (>= 0).

    Used by flags that document 0 as a sentinel (e.g. "0 = no limit",
    "0 = force refetch"). Without this, `type=positive_int` rejects
    the documented 0, and `type=int` accepts confusing negatives that
    either silently slice-from-end (sequences) or match nothing
    (filters).
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"must be an integer, got {value!r}"
        )
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n
