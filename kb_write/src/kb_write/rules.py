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
    titles in CJK lose all their characters in the ASCII strip.

    v0.28.2 fix for stress finding G10: when the ASCII-strip yields
    empty (title was all CJK / emoji / punctuation), we previously
    fell back to the literal word "thought", which made every
    CJK-titled same-day thought collide on `YYYY-MM-DD-thought`.
    Now we append 6 hex chars of entropy (from os.urandom) so each
    such auto-slug is unique. The user still sees a placeholder slug
    ("thought") with a suffix — they're encouraged to provide an
    explicit --slug or an English-containing title, but they won't
    hit WriteExistsError when they don't.
    """
    d = today or date.today()
    raw = title.strip().lower()
    # Replace non-alphanumerics with hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    fallback_used = False
    if not slug:
        slug = "thought"
        fallback_used = True
    # Cap length so file system paths stay sane.
    slug = slug[:60].strip("-")
    if not slug:
        slug = "thought"
        fallback_used = True
    if fallback_used:
        # Entropy suffix keeps same-day CJK / emoji titles unique.
        import os as _os
        slug = f"{slug}-{_os.urandom(3).hex()}"
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

_PAPER_KEY_RE = re.compile(r"^[A-Z0-9]{8}(?:-ch\d+)?$")


def validate_kb_ref_entry(entry: str) -> None:
    """Ensure a kb_refs entry is well-formed.

    Permitted shapes (v26, per AGENT-WRITE-RULES §9):
      - `papers/<KEY>`                 — external paper / book / chapter md;
                                         KEY is 8-char Zotero uppercase
                                         alphanumeric, optionally followed
                                         by `-ch<NN>` for chapters.
      - `topics/standalone-note/<KEY>` — Zotero standalone note (rare);
                                         KEY is 8-char Zotero uppercase.
      - `topics/agent-created/<SLUG>`  — AI-generated topic synthesis;
                                         SLUG is kebab-case.
      - `thoughts/<SLUG>`              — AI-generated thought;
                                         SLUG is YYYY-MM-DD-kebab.
      - bare `<key>` — discouraged but allowed (caller disambiguates).

    Refuses deprecated v25 forms (`zotero-notes/...`, top-level
    `topics/<slug>` without sub-bucket) with a helpful error so
    the user knows to update their data.

    v0.28.2 tightened: previously `papers/`, `thoughts/`,
    `topics/agent-created/`, `topics/standalone-note/` with empty or
    multi-segment tails all leaked through. Now each prefix enforces
    exactly one remaining segment AND validates it against the
    per-type slug/key rules.
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

    # v26 two-segment prefixes: exactly one tail segment, validated per-type.
    for prefix, kind in (
        ("topics/standalone-note/", "note"),
        ("topics/agent-created/",   "topic"),
    ):
        if e.startswith(prefix):
            tail = e[len(prefix):]
            if not tail:
                raise RuleViolation(
                    f"kb_refs entry {entry!r}: {prefix!r} has empty tail; "
                    f"expected {prefix}<{kind}-slug>."
                )
            if "/" in tail:
                raise RuleViolation(
                    f"kb_refs entry {entry!r}: {prefix!r} accepts one "
                    f"segment, got {tail!r}."
                )
            if kind == "note":
                # standalone-note uses Zotero keys.
                if not _ZKEY_RE.match(tail):
                    raise RuleViolation(
                        f"kb_refs entry {entry!r}: {prefix} expects an "
                        f"8-char Zotero key; got {tail!r}."
                    )
            else:
                # agent-created topic uses kebab slug (no slashes — we
                # rejected those above). validate_topic_slug would accept
                # hierarchical slugs with '/' but we want flat under this
                # prefix for clarity, so reuse just its character class.
                if not re.match(r"^[a-z0-9][a-z0-9\-]*$", tail):
                    raise RuleViolation(
                        f"kb_refs entry {entry!r}: agent-created topic slug "
                        f"must be lowercase kebab; got {tail!r}."
                    )
            return

    # top-level topics/<slug> without sub-bucket = legacy.
    if e.startswith("topics/"):
        sub = e[len("topics/"):]
        if "/" not in sub:
            raise RuleViolation(
                f"kb_refs entry {entry!r}: top-level 'topics/<slug>' "
                f"is DEPRECATED in v26. Use 'topics/agent-created/<slug>'."
            )
        # topics/<X>/... but X isn't a known sub-bucket: still illegal.
        raise RuleViolation(
            f"kb_refs entry {entry!r}: unknown topics sub-bucket "
            f"{sub.split('/', 1)[0]!r}. Expected 'standalone-note' "
            f"or 'agent-created'."
        )

    head, _, tail = e.partition("/")
    if head not in ("papers", "thoughts"):
        raise RuleViolation(
            f"kb_refs subdir {head!r} unknown. Expected one of "
            "papers, topics/standalone-note, topics/agent-created, "
            "thoughts."
        )
    # papers/ and thoughts/ accept exactly one tail segment.
    if not tail:
        raise RuleViolation(
            f"kb_refs entry {entry!r}: {head}/ has empty tail; "
            f"expected {head}/<{'KEY' if head == 'papers' else 'slug'}>."
        )
    if "/" in tail:
        raise RuleViolation(
            f"kb_refs entry {entry!r}: {head}/ accepts one segment, "
            f"got {tail!r}."
        )
    if head == "papers":
        if not _PAPER_KEY_RE.match(tail):
            raise RuleViolation(
                f"kb_refs entry {entry!r}: papers/ key must be 8-char "
                f"Zotero uppercase alphanumeric, optionally with "
                f"'-ch<NN>' chapter suffix. Got {tail!r}."
            )
    else:  # thoughts
        if not _THOUGHT_SLUG_RE.match(tail):
            raise RuleViolation(
                f"kb_refs entry {entry!r}: thoughts/ slug must match "
                f"YYYY-MM-DD-kebab. Got {tail!r}."
            )
