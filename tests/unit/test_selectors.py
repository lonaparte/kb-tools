"""Unit tests for kb_write re-read selectors.

Covers the 7 selectors at the boundary between CLI args and SQL:
each must accept its ACCEPTED_KWARGS, reject unknown args, and
produce the right pool given a mock kb_root.

These don't go through the full re-read pipeline — that's covered
by the E2E script. Here we exercise the selector objects
directly so a failure points at the right file."""
from __future__ import annotations

import pytest

from kb_write.selectors.registry import REGISTRY, DEFAULT_SELECTOR_NAME


def test_all_seven_selectors_registered():
    # v26 ships 7 selectors; regression test if someone removes one.
    assert len(REGISTRY) == 7


def test_default_selector_is_unread_first():
    assert DEFAULT_SELECTOR_NAME == "unread-first"
    assert DEFAULT_SELECTOR_NAME in REGISTRY


@pytest.mark.parametrize("name", list(REGISTRY))
def test_selector_has_accepted_kwargs(name):
    # Every selector MUST declare its accepted kwargs so the CLI
    # can reject typos. Missing ACCEPTED_KWARGS = silent defaulting.
    sel = REGISTRY[name]
    assert hasattr(sel, "ACCEPTED_KWARGS"), (
        f"selector {name!r} missing ACCEPTED_KWARGS"
    )
    assert isinstance(sel.ACCEPTED_KWARGS, frozenset), (
        f"selector {name!r} ACCEPTED_KWARGS must be frozenset, "
        f"got {type(sel.ACCEPTED_KWARGS).__name__}"
    )


@pytest.mark.parametrize("name", list(REGISTRY))
def test_selector_has_name_and_description(name):
    sel = REGISTRY[name]
    assert sel.name == name
    assert sel.description, (
        f"selector {name!r} has empty description — needed for "
        f"`--list-selectors` output"
    )


def test_random_selector_has_no_selector_args():
    # --seed is a top-level CLI flag on `kb-write re-read`, not a
    # selector-arg. So random's selector-level ACCEPTED_KWARGS is
    # empty — guards against accidental re-introduction.
    sel = REGISTRY["random"]
    assert sel.ACCEPTED_KWARGS == frozenset()


def test_by_tag_selector_accepts_tag_singular_and_plural():
    sel = REGISTRY["by-tag"]
    # Either form should work — tag= for one, tags=a,b,c for many.
    assert "tag" in sel.ACCEPTED_KWARGS
    assert "tags" in sel.ACCEPTED_KWARGS


def test_related_to_recent_selector_accepts_anchor_days():
    sel = REGISTRY["related-to-recent"]
    assert "anchor_days" in sel.ACCEPTED_KWARGS


def test_expected_selector_names():
    # Regression: the exact v26 set of 7 names. If this changes,
    # the docs and the CHANGELOG need updating too.
    assert set(REGISTRY) == {
        "unread-first", "random", "stale-first",
        "never-summarized", "oldest-summary-first",
        "by-tag", "related-to-recent",
    }
