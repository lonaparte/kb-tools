"""Extract inbound-link candidates from md content.

This module is pure — it knows nothing about SQLite, the filesystem,
or the indexer. Given a frontmatter dict + body text, it returns a
list of `ExtractedRef` candidates. Resolution (turning candidates
into typed edges by looking up whether a key exists in papers/
topics/etc.) happens elsewhere.

Separated as its own module because (a) it's testable in isolation,
(b) the regexes get fiddly, (c) the indexer shouldn't grow any more
than it already has.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ExtractedRef:
    """One reference extracted from an md. Unresolved — the resolver
    looks up whether `key` matches a paper/topic/thought/note.

    When `hint_type` is non-None (from frontmatter with explicit
    subdir, or from `@citekey` syntax), resolution is constrained:
    e.g. `@alice2024` must resolve to a paper (citation_key lookup),
    never a topic. For wikilinks/mdlinks without subdir, hint_type
    is None and resolver tries all node types.
    """
    key: str                     # raw key extracted (e.g. "ABCD1234")
    origin: str                  # frontmatter | wikilink | mdlink | cite
    hint_type: str | None = None  # paper | note | topic | thought | None


# ---------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------

# Obsidian-style [[wikilink]]. Captures the inner text. Allow letters,
# digits, hyphen, underscore, slash (for hierarchical topics like
# "attention/overview"), and dot in the middle (for filenames with
# extensions like "[[FOO.md]]"). Strip a trailing ".md" at extraction.
# Stop at |  to allow [[slug|display text]] form.
# Stop at #  to allow [[slug#heading]] form.
_WIKILINK_RE = re.compile(r"\[\[([^\]\|\#]+?)(?:[\|\#][^\]]*)?\]\]")

# Markdown link [text](path). v26 KB subdirs:
#   - papers/
#   - topics/standalone-note/          (two-segment)
#   - topics/agent-created/            (two-segment)
#   - thoughts/
# Legacy v25 prefixes (zotero-notes/, bare topics/) are also captured
# so that _from_mdlinks can surface them via _classify_subdir_prefix
# for the resolver to decide (hint_type=None for bare topics/ → the
# resolver will most likely mark it dangling, which is the correct
# signal: "v25 content, reorganise").
_MDLINK_RE = re.compile(
    r"\[[^\]]*\]\("
    r"(papers"
    r"|topics/standalone-note"
    r"|topics/agent-created"
    r"|zotero-notes"
    r"|topics"
    r"|thoughts"
    r")"
    r"/([^\)\s]+?)(?:\.md)?\)"
)

# @citation_key — a bibtex-style citation reference. The convention:
# starts with @, followed by letters/digits/underscore, no spaces. We
# also allow the form @{key} to disambiguate where needed.
# Avoid matching email addresses (@) by requiring a word boundary
# before @ AND disallowing '.' right after the key (so @foo.bar
# won't match).
_CITE_RE = re.compile(r"(?:^|[^\w])@([A-Za-z][\w]+)(?!\.\w)(?!\@)")

# Map subdir prefix → node type.
#
# v26 layout: we accept BOTH single-segment (papers/, thoughts/) and
# two-segment (topics/standalone-note/, topics/agent-created/) prefixes.
# Single-segment `topics/` (without a sub-bucket) is a v25 legacy form
# — we DO NOT silently map it to `topic`, because the corresponding
# slug in the DB now lives under topics/agent-created/<slug> and a
# naive "topics/<slug>" won't match the topic_slugs set. Such refs
# become hint_type=None and the resolver either finds them via
# fallback or marks them dangling, making the v25 legacy visible.
_SUBDIR_TWO_SEG_TO_TYPE = {
    "topics/standalone-note": "note",
    "topics/agent-created":   "topic",
}
_SUBDIR_SINGLE_TO_TYPE = {
    "papers":   "paper",
    "thoughts": "thought",
}


def _classify_subdir_prefix(path: str) -> tuple[str | None, str]:
    """Split `<prefix>/<key>` into (hint_type, key).

    Recognises v26's two-segment prefixes first, then falls back to
    single-segment. Returns (None, path) if no prefix matches.
    `path` has been stripped of leading slash and trailing .md by
    the caller.
    """
    # Two-segment first (longest-match).
    for prefix, node_type in _SUBDIR_TWO_SEG_TO_TYPE.items():
        if path.startswith(prefix + "/"):
            return (node_type, path[len(prefix) + 1:])
    # Single-segment.
    if "/" in path:
        head, _, tail = path.partition("/")
        nt = _SUBDIR_SINGLE_TO_TYPE.get(head)
        if nt is not None:
            return (nt, tail)
    return (None, path)


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def extract_refs(
    frontmatter: dict,
    body: str,
    *,
    include_cite: bool = True,
) -> list[ExtractedRef]:
    """Extract all reference candidates from a single md's content.

    Args:
        frontmatter: parsed YAML frontmatter dict.
        body: the md content *without* frontmatter.
        include_cite: whether to scan for @citekey refs. Disable when
            the body is a zotero-note (citekeys there are usually
            about the content of an external paper, not refs between
            KB items).

    Order of returned refs is not significant and includes duplicates;
    callers should dedupe on (src, dst, origin).
    """
    refs: list[ExtractedRef] = []
    refs.extend(_from_frontmatter(frontmatter))
    refs.extend(_from_wikilinks(body))
    refs.extend(_from_mdlinks(body))
    if include_cite:
        refs.extend(_from_cites(body))
    return refs


# ---------------------------------------------------------------------
# Individual extractors
# ---------------------------------------------------------------------

def _from_frontmatter(fm: dict) -> Iterable[ExtractedRef]:
    """Parse kb_refs: list from frontmatter.

    Accepted forms:
      - "papers/ABCD1234"         → hint_type=paper
      - "topics/gfm-stability"    → hint_type=topic
      - "ABCD1234"                → hint_type=None (resolver chooses)

    Anything that isn't a string is silently dropped (defensive).
    """
    raw = fm.get("kb_refs")
    if not raw:
        return
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        # Trim leading slash, accidental quotes.
        s = s.strip().lstrip("/").strip('"\'')
        # Strip trailing .md if present, BEFORE classification so the
        # prefix matcher sees a consistent shape.
        if s.endswith(".md"):
            s = s[:-3]
        # v26: recognise single- or two-segment subdir prefixes.
        hint_type, key = _classify_subdir_prefix(s)
        if not key:
            continue
        yield ExtractedRef(key=key, origin="frontmatter", hint_type=hint_type)


def _from_wikilinks(body: str) -> Iterable[ExtractedRef]:
    """Extract [[key]] or [[key|display]] patterns."""
    for m in _WIKILINK_RE.finditer(body):
        inner = m.group(1).strip()
        if not inner:
            continue
        # Strip .md before classification for consistency with frontmatter.
        if inner.endswith(".md"):
            inner = inner[:-3]
        # v26: single- or two-segment subdir prefix.
        hint_type, key = _classify_subdir_prefix(inner)
        if not key:
            continue
        yield ExtractedRef(key=key, origin="wikilink", hint_type=hint_type)


def _from_mdlinks(body: str) -> Iterable[ExtractedRef]:
    """Extract [text](subdir/key.md) patterns.

    v26: the `subdir` captured by the regex may be a two-segment form
    like "topics/agent-created" — the regex below accommodates that.
    See _MDLINK_RE for the exact pattern. We reconstruct the full
    path and run it through _classify_subdir_prefix.
    """
    for m in _MDLINK_RE.finditer(body):
        subdir = m.group(1)
        key = m.group(2).strip()
        if not key:
            continue
        full = f"{subdir}/{key}"
        if full.endswith(".md"):
            full = full[:-3]
        hint_type, resolved_key = _classify_subdir_prefix(full)
        yield ExtractedRef(
            key=resolved_key, origin="mdlink", hint_type=hint_type,
        )


def _from_cites(body: str) -> Iterable[ExtractedRef]:
    """Extract @citation_key. Always hint_type='paper'."""
    # Strip code blocks first — a lot of email addresses / shell
    # commands live in code and we don't want @root, @user, @echo
    # polluting the graph.
    clean = _strip_code_blocks(body)
    for m in _CITE_RE.finditer(clean):
        key = m.group(1).strip()
        if not key:
            continue
        yield ExtractedRef(key=key, origin="cite", hint_type="paper")


def _strip_code_blocks(text: str) -> str:
    """Remove fenced code blocks and inline code.

    Defensive: `@echo on` inside a shell example shouldn't become
    a link. We don't care about perfect markdown parsing, just
    enough to avoid obvious false positives.
    """
    # Fenced code blocks (``` ... ```).
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # Inline code (`...`).
    text = re.sub(r"`[^`]*`", "", text)
    return text
