"""Shared argparse validators for kb-importer subcommands.

1.4.4: the local copies of `_positive_int` / `_nonnegative_int` were
identical across kb-write, kb-mcp, kb-importer and kb-citations.
Consolidated into `kb_core.argtypes`; kb-importer (and the other
three CLIs) now import from there. Re-exported with the leading
underscore so existing call sites within this package don't change.
"""
from __future__ import annotations

from kb_core.argtypes import (  # noqa: F401
    positive_int as _positive_int,
    nonnegative_int as _nonnegative_int,
)
