"""Pytest config.

Adds src/ paths to sys.path so tests can `import kb_core` / `import
kb_write` / etc. without requiring `pip install -e .` first. Mirrors
what scripts/test_e2e.py does — the two test paths should see the
same module tree.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC_DIRS = [
    REPO / "kb_core/src",
    REPO / "kb_write/src",
    REPO / "kb_mcp/src",
    REPO / "kb_importer/src",
    REPO / "kb_citations/src",
]

for p in _SRC_DIRS:
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
