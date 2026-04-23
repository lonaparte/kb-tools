"""MCP write tools: thin wrappers around `kb_write.ops`.

These let MCP clients (Claude Desktop, etc.) create thoughts/topics,
update AI zones, tag/ref, and so on — WITH THE SAME RULES AND
VALIDATION as local CLI users. Shared protocol, one enforcement
pipeline.

Each wrapper:
- Constructs a WriteContext (git_commit defaults to True per user
  policy; can be overridden via the tool args).
- Calls the kb_write op.
- Formats the WriteResult as a short human-readable string for the
  AI to see.

If kb_write isn't importable (e.g. kb-mcp installed without
kb-write), these raise at import — kb-mcp's server.py should guard
the imports so the tools are simply unavailable rather than
crashing the whole server.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

# Import kb_write eagerly; server.py catches ImportError on the whole
# module so the MCP server still starts if kb_write isn't installed.
from kb_write.config import WriteContext
from kb_write.ops import (
    thought as thought_ops,
    topic as topic_ops,
    preference as pref_ops,
    ai_zone as ai_zone_ops,
    tag as tag_ops,
    ref as ref_ops,
    delete as delete_ops,
    doctor as doctor_ops,
)
from kb_write.rules import RuleViolation
from kb_write.atomic import WriteConflictError, WriteExistsError
from kb_write.zones import ZoneError
from kb_write.frontmatter import FrontmatterError


def _ctx(kb_root: Path, *, git_commit: bool = True, reindex: bool = True) -> WriteContext:
    return WriteContext(
        kb_root=kb_root,
        git_commit=git_commit,
        reindex=reindex,
        lock=True,
        # Stamp MCP-originated writes so the audit log distinguishes
        # them from CLI / Python API writes.
        actor="mcp",
    )


def _format_result(result) -> str:
    lines = [
        f"✓ {result.address.node_type}/{result.address.key}",
        f"  path:  {result.address.md_rel_path}",
        f"  mtime: {result.mtime:.9f}",
    ]
    if result.git_sha:
        lines.append(f"  git:   {result.git_sha[:12]}")
    if result.reindexed:
        lines.append("  reindex: ok")
    return "\n".join(lines)


def _catch(fn):
    """Turn kb_write exceptions into user-facing error strings so MCP
    responses are always well-formed. Unexpected exceptions still
    propagate so the server surfaces them."""
    try:
        return fn()
    except RuleViolation as e:
        return f"[rule violation] {e}"
    except WriteConflictError as e:
        return f"[conflict] {e}"
    except WriteExistsError as e:
        return f"[exists] {e}"
    except ZoneError as e:
        return f"[ai-zone error] {e}"
    except FrontmatterError as e:
        # FrontmatterError means the md file exists but its YAML
        # frontmatter is malformed / unparseable. Distinct from
        # "file not found" — conflating the two sent users on
        # wild-goose chases when the real fix is editing their YAML.
        return f"[frontmatter parse error] {e}"
    except FileNotFoundError as e:
        return f"[not found] {e}"


# ----------------------------------------------------------------------
# thought
# ----------------------------------------------------------------------

def create_thought_impl(
    kb_root: Path, title: str, body: str,
    slug: str | None = None,
    refs: Sequence[str] = (),
    tags: Sequence[str] = (),
    git_commit: bool = True,
) -> str:
    def _run():
        result = thought_ops.create(
            _ctx(kb_root, git_commit=git_commit),
            title=title, body=body, slug=slug,
            refs=refs, tags=tags,
        )
        return _format_result(result)
    return _catch(_run)


def update_thought_impl(
    kb_root: Path, target: str, expected_mtime: float,
    body: str | None = None, title: str | None = None,
    refs: Sequence[str] | None = None, tags: Sequence[str] | None = None,
    refs_mode: str = "replace", tags_mode: str = "replace",
    git_commit: bool = True,
) -> str:
    def _run():
        result = thought_ops.update(
            _ctx(kb_root, git_commit=git_commit),
            target, expected_mtime=expected_mtime,
            body=body, title=title,
            refs=refs, tags=tags,
            refs_mode=refs_mode, tags_mode=tags_mode,
        )
        return _format_result(result)
    return _catch(_run)


# ----------------------------------------------------------------------
# topic
# ----------------------------------------------------------------------

def create_topic_impl(
    kb_root: Path, slug: str, title: str, body: str,
    refs: Sequence[str] = (), tags: Sequence[str] = (),
    git_commit: bool = True,
) -> str:
    def _run():
        result = topic_ops.create(
            _ctx(kb_root, git_commit=git_commit),
            slug=slug, title=title, body=body,
            refs=refs, tags=tags,
        )
        return _format_result(result)
    return _catch(_run)


def update_topic_impl(
    kb_root: Path, target: str, expected_mtime: float,
    body: str | None = None, title: str | None = None,
    refs: Sequence[str] | None = None, tags: Sequence[str] | None = None,
    refs_mode: str = "replace", tags_mode: str = "replace",
    git_commit: bool = True,
) -> str:
    def _run():
        result = topic_ops.update(
            _ctx(kb_root, git_commit=git_commit),
            target, expected_mtime=expected_mtime,
            body=body, title=title,
            refs=refs, tags=tags,
            refs_mode=refs_mode, tags_mode=tags_mode,
        )
        return _format_result(result)
    return _catch(_run)


# ----------------------------------------------------------------------
# preference
# ----------------------------------------------------------------------

def create_preference_impl(
    kb_root: Path, slug: str, body: str,
    scope: str = "global", priority: int = 50,
    title: str | None = None,
    git_commit: bool = True,
) -> str:
    def _run():
        result = pref_ops.create(
            _ctx(kb_root, git_commit=git_commit, reindex=False),
            slug=slug, body=body, scope=scope,
            priority=priority, title=title,
        )
        return _format_result(result)
    return _catch(_run)


def update_preference_impl(
    kb_root: Path, slug: str, expected_mtime: float,
    body: str | None = None,
    scope: str | None = None,
    priority: int | None = None,
    title: str | None = None,
    git_commit: bool = True,
) -> str:
    def _run():
        result = pref_ops.update(
            _ctx(kb_root, git_commit=git_commit, reindex=False),
            slug=slug, expected_mtime=expected_mtime,
            body=body, scope=scope, priority=priority, title=title,
        )
        return _format_result(result)
    return _catch(_run)


# ----------------------------------------------------------------------
# AI zone
# ----------------------------------------------------------------------

def append_ai_zone_impl(
    kb_root: Path, target: str, expected_mtime: float,
    *,
    title: str, body: str,
    entry_date: str | None = None,
    git_commit: bool = True,
) -> str:
    """v26: append a dated entry to the AI zone (replaces v25's
    update_ai_zone_impl, which did full-body replace). Called from
    the MCP tool `append_ai_zone`.
    """
    from datetime import date as _date

    def _run():
        ed = _date.fromisoformat(entry_date) if entry_date else None
        result = ai_zone_ops.append(
            _ctx(kb_root, git_commit=git_commit),
            target,
            expected_mtime=expected_mtime,
            title=title,
            body=body,
            entry_date=ed,
        )
        return _format_result(result)
    return _catch(_run)


def read_ai_zone_impl(kb_root: Path, target: str) -> str:
    def _run():
        body, mtime = ai_zone_ops.read_zone(kb_root, target)
        return f"<!-- mtime: {mtime:.9f} -->\n{body}"
    return _catch(_run)


# ----------------------------------------------------------------------
# tag / ref
# ----------------------------------------------------------------------

def add_kb_tag_impl(
    kb_root: Path, target: str, tag: str,
    expected_mtime: float | None = None,
    git_commit: bool = True,
) -> str:
    def _run():
        result = tag_ops.add(
            _ctx(kb_root, git_commit=git_commit),
            target, tag, expected_mtime=expected_mtime,
        )
        return _format_result(result)
    return _catch(_run)


def remove_kb_tag_impl(
    kb_root: Path, target: str, tag: str,
    expected_mtime: float | None = None,
    git_commit: bool = True,
) -> str:
    def _run():
        result = tag_ops.remove(
            _ctx(kb_root, git_commit=git_commit),
            target, tag, expected_mtime=expected_mtime,
        )
        return _format_result(result)
    return _catch(_run)


def add_kb_ref_impl(
    kb_root: Path, target: str, ref: str,
    expected_mtime: float | None = None,
    git_commit: bool = True,
) -> str:
    def _run():
        result = ref_ops.add(
            _ctx(kb_root, git_commit=git_commit),
            target, ref, expected_mtime=expected_mtime,
        )
        return _format_result(result)
    return _catch(_run)


def remove_kb_ref_impl(
    kb_root: Path, target: str, ref: str,
    expected_mtime: float | None = None,
    git_commit: bool = True,
) -> str:
    def _run():
        result = ref_ops.remove(
            _ctx(kb_root, git_commit=git_commit),
            target, ref, expected_mtime=expected_mtime,
        )
        return _format_result(result)
    return _catch(_run)


# ----------------------------------------------------------------------
# delete
# ----------------------------------------------------------------------

def delete_node_impl(
    kb_root: Path, target: str, confirm: bool = False,
    git_commit: bool = True,
) -> str:
    if not confirm:
        return (
            "[refused] delete requires confirm=True. "
            "This is an intentional guard. Pass confirm=True to proceed."
        )
    def _run():
        result = delete_ops.delete(
            _ctx(kb_root, git_commit=git_commit),
            target, confirm=True,
        )
        return (
            f"deleted {result.address.node_type}/{result.address.key}\n"
            f"  git: {result.git_sha[:12] if result.git_sha else '(not committed)'}"
        )
    return _catch(_run)


# ----------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------

def doctor_impl(kb_root: Path, fix: bool = False) -> str:
    def _run():
        ctx = _ctx(kb_root, git_commit=False, reindex=False)
        report = doctor_ops.doctor(ctx, fix=fix)
        return doctor_ops.format_report(report)
    return _catch(_run)
