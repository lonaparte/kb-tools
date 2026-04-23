"""Validation rules. Enforces AGENT-WRITE-RULES.md at the boundary.

Every write op in `kb_write.ops.*` funnels through one of the
validators here so we have a single source of truth for "what's
allowed".

Validators don't mutate anything — they either return the normalized
input or raise `RuleViolation`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


# Single source of truth for KB-protected frontmatter fields.
# frontmatter.py imports these constants from here (not the other way
# around) — rules.py has to be importable without the third-party
# `python-frontmatter` package so CLI error messages can mention
# protected fields even when the parser isn't installed. An older
# version of this file claimed these constants were "duplicated from
# frontmatter.py" — that comment predated the single-source
# refactor and was misleading. There is ONE canonical copy; it
# lives here.
#
# Protected fields are those only Zotero or the fulltext pipeline
# may set. Hand-edits / agent writes that touch these keys raise
# RuleViolation; update ops strip them silently.
PROTECTED_PREFIXES = ("zotero_", "fulltext_")
PROTECTED_FIELDS = frozenset({
    "kind", "item_type", "title", "authors", "year",
    "doi", "publication", "citation_key", "abstract",
})


class RuleViolation(Exception):
    """Raised when a proposed write would violate the KB rules.

    Message is user-facing; callers should propagate without
    wrapping.
    """


# Dated slug: "YYYY-MM-DD-anything". kb-importer uses this convention
# for child note filenames too; re-using it for thoughts makes it
# trivial to sort chronologically.
_THOUGHT_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9\-]*$")
_TOPIC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*(/[a-z0-9][a-z0-9\-]*)*$")
# Zotero keys: 8 uppercase alphanum.
_ZKEY_RE = re.compile(r"^[A-Z0-9]{8}$")


# --------------------------------------------------------------
# Slug validation
# --------------------------------------------------------------

def validate_thought_slug(slug: str) -> None:
    if not _THOUGHT_SLUG_RE.match(slug):
        raise RuleViolation(
            f"thought slug {slug!r} does not match YYYY-MM-DD-name "
            f"(lowercase, hyphens; no spaces/underscores). "
            f"Example: 2026-04-22-passivity-gfm-link."
        )


def validate_topic_slug(slug: str) -> None:
    if not _TOPIC_SLUG_RE.match(slug):
        raise RuleViolation(
            f"topic slug {slug!r} must be kebab-case, optionally "
            f"hierarchical with '/'. Example: gfm-stability or "
            f"attention/overview."
        )


def validate_zotero_key(key: str) -> None:
    if not _ZKEY_RE.match(key):
        raise RuleViolation(
            f"Zotero key {key!r} must be 8 uppercase alphanumeric chars."
        )


# --------------------------------------------------------------
# Slug generation
# --------------------------------------------------------------

def make_thought_slug(title: str, *, today: date | None = None) -> str:
    """Generate a valid thought slug from a title.

    Strategy: kebab-case the title (ASCII + digits + hyphens only),
    prepend today's date. Drops non-ASCII characters silently —
    titles in CJK become empty-slugged so caller gets
    `2026-04-22-thought` (with a fallback word). Encourage agents
    to either provide a descriptive English title OR explicit slug.
    """
    d = today or date.today()
    raw = title.strip().lower()
    # Replace non-alphanumerics with hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not slug:
        slug = "thought"
    # Cap length so file system paths stay sane.
    slug = slug[:60].strip("-") or "thought"
    return f"{d.isoformat()}-{slug}"


# --------------------------------------------------------------
# Frontmatter validation
# --------------------------------------------------------------

@dataclass
class FrontmatterProposal:
    """Wraps a dict that a caller wants to apply as frontmatter.
    `validate` raises on any rule violation and returns the filtered
    proposal (protected fields removed)."""

    fields: dict

    def filtered(self) -> dict:
        """Return fields with protected keys removed. Raises if the
        proposal tries to set a protected field to something other
        than its current value (see validate for context)."""
        out = {}
        for k, v in self.fields.items():
            if _is_protected(k):
                continue
            out[k] = v
        return out


def validate_agent_fields(proposal: dict) -> dict:
    """Strict variant: raise RuleViolation if the proposal contains
    protected fields at all. Use when the agent was explicitly trying
    to set those fields (e.g. via --set); a silent drop would be
    confusing.
    """
    violations = []
    for k in proposal:
        if _is_protected(k):
            violations.append(k)
    if violations:
        raise RuleViolation(
            f"Cannot set protected fields: {violations}. "
            f"These are owned by kb-importer."
        )
    return proposal


def _is_protected(key: str) -> bool:
    if key in PROTECTED_FIELDS:
        return True
    return any(key.startswith(p) for p in PROTECTED_PREFIXES)


# --------------------------------------------------------------
# Subdir / path validation
# --------------------------------------------------------------

def validate_not_outside_kb(kb_root: Path, candidate: Path) -> None:
    """Raise RuleViolation if `candidate` is not inside kb_root.

    Delegates heavy lifting to pathlib. Kept here so ops layer has
    a rule-named entry point rather than an ad-hoc path check.
    """
    try:
        candidate.resolve().relative_to(kb_root.resolve())
    except ValueError:
        raise RuleViolation(
            f"path {candidate} is outside the KB root {kb_root}."
        )


# --------------------------------------------------------------
# kb_refs entry validation
# --------------------------------------------------------------

def validate_kb_ref_entry(entry: str) -> None:
    """Ensure a kb_refs entry is well-formed.

    Permitted shapes (v26, per AGENT-WRITE-RULES §9):
      - `papers/<KEY>`                 — external paper / book / chapter md
      - `topics/standalone-note/<KEY>` — Zotero standalone note (rare)
      - `topics/agent-created/<SLUG>`  — AI-generated topic synthesis
      - `thoughts/<SLUG>`              — AI-generated thought
      - bare `<key>` — discouraged but allowed (caller disambiguates)

    Refuses deprecated v25 forms (`zotero-notes/...`, top-level
    `topics/<slug>` without sub-bucket) with a helpful error so
    the user knows to update their data.
    """
    if not isinstance(entry, str):
        raise RuleViolation(f"kb_refs entry is not a string: {entry!r}")
    e = entry.strip()
    if not e:
        raise RuleViolation("empty kb_refs entry")
    if e.startswith("/") or ".." in e.split("/"):
        raise RuleViolation(
            f"invalid kb_refs entry {entry!r}: absolute paths and "
            "'..' segments are forbidden."
        )
    if "/" not in e:
        return  # bare key, OK

    # Deprecated v25 shapes get caught first with pointed messages.
    if e.startswith("zotero-notes/"):
        raise RuleViolation(
            f"kb_refs entry {entry!r}: 'zotero-notes/' is DEPRECATED "
            f"in v26. Use 'topics/standalone-note/<KEY>' instead."
        )

    # v26 two-segment prefixes first.
    if e.startswith("topics/standalone-note/") or e.startswith("topics/agent-created/"):
        return
    # top-level topics/<slug> without sub-bucket = legacy.
    if e.startswith("topics/"):
        sub = e[len("topics/"):]
        if "/" not in sub:
            raise RuleViolation(
                f"kb_refs entry {entry!r}: top-level 'topics/<slug>' "
                f"is DEPRECATED in v26. Use 'topics/agent-created/<slug>'."
            )

    head = e.split("/", 1)[0]
    if head not in ("papers", "thoughts"):
        raise RuleViolation(
            f"kb_refs subdir {head!r} unknown. Expected one of "
            "papers, topics/standalone-note, topics/agent-created, "
            "thoughts."
        )
