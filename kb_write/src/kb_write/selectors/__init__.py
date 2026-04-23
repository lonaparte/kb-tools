"""Pluggable selectors for `kb-write re-read`.

Public surface:
  - PaperInfo   — dataclass passed to selectors
  - Selector    — Protocol each selector implements
  - REGISTRY    — {name: selector} lookup
  - DEFAULT_SELECTOR_NAME
  - describe_all()
  - parse_selector_args()
"""
from __future__ import annotations

from .base import PaperInfo, Selector, parse_selector_args
from .registry import REGISTRY, DEFAULT_SELECTOR_NAME, describe_all


__all__ = [
    "PaperInfo", "Selector", "parse_selector_args",
    "REGISTRY", "DEFAULT_SELECTOR_NAME", "describe_all",
]
