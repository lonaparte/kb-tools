"""get_agent_preferences: surface the user's persistent preferences
to an MCP client at session start.

The preferences live at `<kb_root>/.agent-prefs/*.md`. kb-importer
and kb-mcp's indexer deliberately skip dot-prefixed directories, so
those files aren't in any SQLite table. We just scan the filesystem
each time this tool is called — it's a small directory (typically
5-15 files).

If `kb_write` happens to be installed, we delegate to its
preference module for format parity. Otherwise we fall back to a
minimal local reader so kb-mcp still works without kb_write.
"""
from __future__ import annotations

from pathlib import Path


def get_agent_preferences_impl(kb_root: Path, scope: str = "all") -> str:
    """Return all preference file content, optionally filtered by scope.

    Prefer delegating to kb_write.ops.preference.read_all_for_agent
    when kb_write is importable. That way the formatting and scope
    logic stays in one place.
    """
    try:
        from kb_write.ops.preference import read_all_for_agent
        return read_all_for_agent(kb_root, scope=scope)
    except ImportError:
        return _fallback_read(kb_root, scope=scope)


def _fallback_read(kb_root: Path, scope: str = "all") -> str:
    """Minimal reader used when kb_write isn't installed."""
    prefs_dir = kb_root / ".agent-prefs"
    if not prefs_dir.exists():
        return (
            "# Agent Preferences\n\n"
            "No .agent-prefs/ directory found in this KB. The user "
            "has not recorded any persistent preferences. "
            "Offer to save any 'remember to...' instructions via "
            "`kb-write pref add` (if the kb-write tool is available)."
        )
    import frontmatter
    sections = ["# Agent Preferences", ""]
    files = sorted(prefs_dir.glob("*.md"))
    # Skip README; it's self-explanatory and not a pref.
    files = [f for f in files if f.name.lower() != "readme.md"]
    if not files:
        sections.append("(directory exists but contains no pref files)")
        return "\n".join(sections)
    for f in files:
        try:
            post = frontmatter.load(str(f))
            fm = post.metadata
            f_scope = str(fm.get("scope", "global"))
            if scope != "all" and f_scope.lower() != scope.lower():
                continue
            priority = fm.get("priority", 50)
            updated = fm.get("last_updated", "")
            title = fm.get("title", f.stem)
            sections.append(
                f"## [{f_scope}, priority={priority}, updated={updated}] {title}"
            )
            sections.append(f"*source: `.agent-prefs/{f.name}`*")
            sections.append("")
            sections.append(f.read_text(encoding="utf-8"))
            sections.append("")
            sections.append("---")
            sections.append("")
        except Exception as e:
            sections.append(f"## {f.name}  [READ ERROR: {e}]\n")
    return "\n".join(sections).rstrip()
