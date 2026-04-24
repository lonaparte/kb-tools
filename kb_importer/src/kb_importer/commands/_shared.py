"""Shared argparse validators for kb-importer subcommands.

Matches the pattern in `kb_write/commands/_shared.py` — each package
keeps its own small copy of these helpers rather than pulling in a
dependency on another package just for argparse type= callables.
"""
from __future__ import annotations

import argparse


def _positive_int(value: str) -> int:
    """argparse `type=` helper: accept positive ints (>= 1)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"must be an integer, got {value!r}"
        )
    if n <= 0:
        raise argparse.ArgumentTypeError(
            f"must be positive, got {n}"
        )
    return n


def _nonnegative_int(value: str) -> int:
    """argparse `type=` helper: accept zero and positive ints (>= 0).

    Used by flags that document 0 as a sentinel (e.g. "0 = no limit",
    "0 = force refetch"). Without this, `type=_positive_int` rejects
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
        raise argparse.ArgumentTypeError(
            f"must be >= 0, got {n}"
        )
    return n
