"""Preference operations — managing files under `.agent-prefs/`.

Preferences are persistent meta-instructions: "escape LaTeX
underscores", "research area is GFM stability", "always reply in
Chinese for this user". They live outside the indexed KB
(.agent-prefs/ starts with a dot, so kb-importer and kb-mcp skip
it) but inside the repo so they version-control and sync across
machines.

These ops differ from thought/topic in two ways:

1. Target directory is `.agent-prefs/`, not one of the indexed
   subdirs — we don't go through paths.parse_target (which rejects
   unknown subdirs).
2. No reindex after write: kb-mcp doesn't index these. Still
   git-commit by default for version control.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import frontmatter

from ..atomic import atomic_write, write_lock
from ..config import WriteContext
from ..frontmatter import read_md, write_md, merge_kb_fields
from ..git import auto_commit
from ..rules import RuleViolation, validate_topic_slug
from .thought import WriteResult, _nullcontext, _record_audit


AGENT_PREFS_DIR = ".agent-prefs"
# Allowed frontmatter scopes the agent should recognize. We don't
# hard-restrict — users can invent new scopes — but this is the
# documented set.
COMMON_SCOPES = (
    "global",
    "writing",
    "research",
    "ai-summary",
    "code",
    "communication",
)


@dataclass(frozen=True)
class PrefAddress:
    """Simple wrapper; prefs are identified by slug only."""
    slug: str

    @property
    def md_rel_path(self) -> str:
        return f"{AGENT_PREFS_DIR}/{self.slug}.md"

    def md_abspath(self, kb_root: Path) -> Path:
        return (kb_root / self.md_rel_path).resolve()


def _validate_pref_slug(slug: str) -> None:
    # Same shape as topic slugs but without hierarchical `/`.
    if "/" in slug:
        raise RuleViolation(
            f"preference slug {slug!r} cannot contain '/'. "
            "Preferences live in a flat directory."
        )
    # Topic-slug regex handles the base case (kebab lower).
    validate_topic_slug(slug)


# ---------------------------------------------------------------------
# Create / add
# ---------------------------------------------------------------------

def create(
    ctx: WriteContext,
    slug: str,
    body: str,
    *,
    scope: str = "global",
    priority: int = 50,
    title: str | None = None,
    extra_frontmatter: dict | None = None,
) -> WriteResult:
    """Create a new preference file at .agent-prefs/<slug>.md.

    Args:
        slug: kebab-case identifier (e.g. 'writing-style').
        body: md body text explaining the preference.
        scope: semantic scope tag (see COMMON_SCOPES; not enforced).
        priority: 0-100; higher wins on conflict.
        title: optional; defaults to "<slug> preferences".
        extra_frontmatter: additional fields (kb_* style is fine).
    """
    _validate_pref_slug(slug)
    if not 0 <= priority <= 100:
        raise RuleViolation(f"priority must be 0..100, got {priority}")

    addr = PrefAddress(slug)
    target = addr.md_abspath(ctx.kb_root)

    fm: dict = {
        "kind": "preference",
        "title": (title or f"{slug} preferences").strip(),
        "scope": scope,
        "priority": priority,
        "last_updated": date.today().isoformat(),
    }
    if extra_frontmatter:
        # merge_kb_fields silently drops protected prefixes; that's
        # fine since protected == zotero_/fulltext_/paper-metadata
        # fields, none of which belong in prefs.
        fm = merge_kb_fields(fm, extra_frontmatter)

    # Ensure .agent-prefs/ exists with README on first use.
    ensure_prefs_dir(ctx.kb_root)

    if ctx.dry_run:
        return WriteResult(
            address=_wrap_addr(addr), md_path=target, mtime=0.0
        )

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        mtime = write_md(
            target, fm, body,
            expected_mtime=None, create_only=True,
        )
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [target],
                op="create_preference",
                target=addr.md_rel_path,
                message_body=ctx.commit_message,
                enabled=True,
            )
    # No reindex for prefs — they're outside the indexed tree.
    _record_audit(
        ctx, op="create_preference", target=addr.md_rel_path,
        mtime_after=mtime, git_sha=sha, reindexed=False,
        extra={"scope": scope, "priority": priority},
    )
    return WriteResult(
        address=_wrap_addr(addr),
        md_path=target,
        mtime=mtime,
        git_sha=sha,
        reindexed=False,
    )


def update(
    ctx: WriteContext,
    slug: str,
    expected_mtime: float,
    *,
    body: str | None = None,
    scope: str | None = None,
    priority: int | None = None,
    title: str | None = None,
    extra_frontmatter: dict | None = None,
) -> WriteResult:
    """Update an existing preference file."""
    _validate_pref_slug(slug)
    addr = PrefAddress(slug)
    md_path = addr.md_abspath(ctx.kb_root)

    existing_fm, existing_body, _ = read_md(md_path)

    new_fm = dict(existing_fm)
    if title is not None:
        new_fm["title"] = title.strip()
    if scope is not None:
        new_fm["scope"] = scope
    if priority is not None:
        if not 0 <= priority <= 100:
            raise RuleViolation(f"priority must be 0..100, got {priority}")
        new_fm["priority"] = priority
    # Always bump last_updated on any update.
    new_fm["last_updated"] = date.today().isoformat()
    if extra_frontmatter:
        new_fm = merge_kb_fields(new_fm, extra_frontmatter)

    new_body = existing_body if body is None else body

    if ctx.dry_run:
        return WriteResult(
            address=_wrap_addr(addr), md_path=md_path, mtime=0.0
        )

    # No-op detection: compare everything EXCEPT last_updated (which
    # we always bump). If nothing user-visible changed, don't rewrite.
    def _without_ts(fm: dict) -> dict:
        return {k: v for k, v in fm.items() if k != "last_updated"}

    if (_without_ts(new_fm) == _without_ts(existing_fm)
            and new_body.rstrip() == existing_body.rstrip()):
        if expected_mtime is not None:
            from ..atomic import assert_mtime_unchanged
            assert_mtime_unchanged(md_path, expected_mtime)
        return WriteResult(
            address=_wrap_addr(addr), md_path=md_path,
            mtime=md_path.stat().st_mtime,
            git_sha=None, reindexed=False,
        )

    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        mtime = write_md(
            md_path, new_fm, new_body,
            expected_mtime=expected_mtime,
        )
        sha = None
        if ctx.git_commit:
            sha = auto_commit(
                ctx.kb_root, [md_path],
                op="update_preference",
                target=addr.md_rel_path,
                message_body=ctx.commit_message,
                enabled=True,
            )
    _record_audit(
        ctx, op="update_preference", target=addr.md_rel_path,
        mtime_before=expected_mtime, mtime_after=mtime,
        git_sha=sha, reindexed=False,
    )
    return WriteResult(
        address=_wrap_addr(addr),
        md_path=md_path,
        mtime=mtime,
        git_sha=sha,
        reindexed=False,
    )


# ---------------------------------------------------------------------
# List / show
# ---------------------------------------------------------------------

def list_all(kb_root: Path) -> list[dict]:
    """Return metadata for every pref file.

    Each item: {slug, title, scope, priority, last_updated, path, mtime}.
    Ordered by priority desc, then last_updated desc.
    """
    prefs_dir = kb_root / AGENT_PREFS_DIR
    if not prefs_dir.exists():
        return []
    results = []
    for md in sorted(prefs_dir.glob("*.md")):
        if md.name.startswith(".") or md.name == "README.md":
            continue
        try:
            post = frontmatter.load(str(md))
            fm = post.metadata
            results.append({
                "slug": md.stem,
                "title": fm.get("title", md.stem),
                "scope": fm.get("scope", "global"),
                "priority": int(fm.get("priority", 50)),
                "last_updated": fm.get("last_updated", ""),
                "path": str(md.relative_to(kb_root)),
                "mtime": md.stat().st_mtime,
            })
        except Exception:
            # Skip corrupt files silently — doctor will surface them.
            continue
    # Priority descending, then last_updated descending. Python's
    # sort is stable, so we sort twice (least-significant first) to
    # get both keys in descending order — can't negate strings, and
    # Python has no `reverse=True` per key.
    results.sort(key=lambda p: (p["last_updated"] or ""), reverse=True)
    results.sort(key=lambda p: p["priority"], reverse=True)
    return results


def read_all_for_agent(kb_root: Path, scope: str = "all") -> str:
    """Return concatenated pref file contents, annotated with
    filename + scope, suitable for showing an agent as system
    context.

    If scope != "all", only files whose frontmatter declares that
    scope (case-insensitive) are included.
    """
    prefs = list_all(kb_root)
    if not prefs:
        return (
            "# Agent Preferences\n\n"
            "No preference files yet. The user has not recorded any "
            "persistent preferences. Proceed with sensible defaults; "
            "offer to save any 'remember to...' instructions via "
            "`kb-write pref add`."
        )

    sections = [
        "# Agent Preferences",
        "",
        "The following are the user's persistent preferences. Apply "
        "them silently unless a conflict arises. In-conversation "
        "instructions always win.",
        "",
    ]
    for p in prefs:
        if scope != "all" and p["scope"].lower() != scope.lower():
            continue
        path = kb_root / p["path"]
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            sections.append(f"## {p['slug']}  [READ ERROR: {e}]\n")
            continue
        sections.append(
            f"## [{p['scope']}, priority={p['priority']}, "
            f"updated={p['last_updated']}] {p['title']}"
        )
        sections.append(f"*source: `{p['path']}`*")
        sections.append("")
        sections.append(text)
        sections.append("")
        sections.append("---")
        sections.append("")
    return "\n".join(sections).rstrip()


# ---------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------

def ensure_prefs_dir(kb_root: Path) -> None:
    """Create .agent-prefs/ and its README if missing. Idempotent."""
    prefs_dir = kb_root / AGENT_PREFS_DIR
    prefs_dir.mkdir(parents=True, exist_ok=True)
    readme = prefs_dir / "README.md"
    if readme.exists():
        return
    from importlib import resources
    template = (
        resources.files("kb_write.scaffold") / "agent_prefs_README.md"
    ).read_text(encoding="utf-8")
    atomic_write(readme, template)


# ---------------------------------------------------------------------
# Internal: wrap PrefAddress in a NodeAddress-shaped object so
# WriteResult typing works. We don't add "preference" to NodeAddress
# node types because it's not an indexed node.
# ---------------------------------------------------------------------

def _wrap_addr(p: PrefAddress):
    """Return an object with .md_rel_path for the WriteResult."""
    from ..paths import NodeAddress
    # Reuse NodeAddress but with a dedicated pseudo-type string. The
    # dataclass is frozen; construct via the standard path. Note
    # this is the only place we emit a node_type that isn't in the
    # canonical set — downstream code that type-dispatches should
    # match by string equality.
    return NodeAddress(node_type="preference", key=p.slug)
