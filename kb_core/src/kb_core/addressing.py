"""NodeAddress abstraction and path ↔ address round-trip.

`NodeAddress(node_type, key)` is the canonical in-memory form for
every KB node. `parse_target` converts user / agent input into one;
`from_md_path` does the reverse from a filesystem path.

Previously lived in kb_write.paths. In v27 moved here so kb_mcp
and kb_importer can consume the same abstraction without importing
kb_write.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import PathError


# Map node type → subdirectory name (v26 layout).
_TYPE_TO_DIR = {
    "paper":   "papers",
    "note":    "topics/standalone-note",
    "topic":   "topics/agent-created",
    "thought": "thoughts",
}

# Reverse map used by from_md_path(). Two values contain a `/`,
# so we can't just flip the dict — callers match the full prefix.
_DIR_TO_TYPE = {
    "papers":                    "paper",
    "topics/standalone-note":    "note",
    "topics/agent-created":      "topic",
    "thoughts":                  "thought",
}


@dataclass(frozen=True)
class NodeAddress:
    """Canonical address of a KB node.

    node_type: one of paper | note | topic | thought | preference
    key:       paper/note → Zotero key; topic/thought → slug;
               preference → filename stem under `.agent-prefs/`.
               Topics may contain `/` for hierarchy (e.g.
               "attention/overview" → topics/agent-created/attention/overview.md).
    """
    node_type: str
    key: str

    @property
    def md_rel_path(self) -> str:
        """Path relative to the kb_root, POSIX-style."""
        # `preference` is a pseudo-type used for files under
        # .agent-prefs/. It isn't an indexed KB node (kb-mcp skips
        # dot-prefixed dirs), but we share the NodeAddress data
        # class to keep WriteResult uniform across write ops.
        if self.node_type == "preference":
            return f".agent-prefs/{self.key}.md"
        subdir = _TYPE_TO_DIR[self.node_type]
        return f"{subdir}/{self.key}.md"

    def md_abspath(self, kb_root: Path) -> Path:
        return (kb_root / self.md_rel_path).resolve()


def parse_target(target: str) -> NodeAddress:
    """Parse an address string like "papers/ABCD1234" or
    "topics/agent-created/gfm-stability" or singular-type shortcut.

    Accepted forms (v26):
      - `papers/<KEY>`                     (unchanged)
      - `topics/standalone-note/<KEY>`     (was zotero-notes/<KEY> in v25)
      - `topics/agent-created/<SLUG>`      (was topics/<SLUG> in v25)
      - `thoughts/<SLUG>`                  (unchanged)
      - `<type>/<key>` — singular shortcut (paper/ABCD, note/X,
        topic/Y, thought/Z)
      - Any of the above with trailing `.md` tolerated
      - For agent-created topics, `<SLUG>` may include `/` for
        hierarchy, e.g. `topics/agent-created/stability/overview`

    Rejects (v26 strict):
      - Legacy v25 paths `zotero-notes/X` and top-level `topics/X`
        (where X has no sub-bucket prefix like agent-created/).
        These are deprecated — the error message points at the new
        location. Content is NOT auto-migrated.
    """
    t = target.strip().strip("/")
    if not t:
        raise PathError("target is empty")
    if t.endswith(".md"):
        t = t[:-3]
    if "/" not in t:
        raise PathError(
            f"{target!r} has no subdir prefix. "
            f"Use 'papers/KEY', 'topics/agent-created/SLUG', "
            f"'topics/standalone-note/KEY', or 'thoughts/SLUG'."
        )

    # v26: detect legacy v25 paths and refuse with a helpful error.
    if t.startswith("zotero-notes/"):
        raise PathError(
            f"{target!r}: 'zotero-notes/' is DEPRECATED in v26. "
            f"Standalone Zotero notes now live under "
            f"'topics/standalone-note/'. Content at the old path "
            f"is not auto-migrated — the user needs to reorganise."
        )

    # Try two-segment prefixes first (longest-match), then one-segment.
    for prefix in ("topics/standalone-note", "topics/agent-created"):
        if t.startswith(prefix + "/"):
            node_type = _DIR_TO_TYPE[prefix]
            tail = t[len(prefix) + 1:]
            if not tail:
                raise PathError(f"{target!r} has no key after subdir")
            if ".." in tail.split("/"):
                raise PathError(f"{target!r}: '..' not allowed in key")
            return NodeAddress(node_type=node_type, key=tail)

    head, _, tail = t.partition("/")

    # v26: bare `topics/<slug>` (no sub-bucket) is a v25 relic.
    if head == "topics":
        raise PathError(
            f"{target!r}: top-level 'topics/<slug>' is DEPRECATED "
            f"in v26. AI-generated topics now live under "
            f"'topics/agent-created/<slug>'. Content at the old "
            f"path is not auto-migrated."
        )

    # Normal single-segment prefixes: papers/, thoughts/, or singular
    # type shortcut (paper/, note/, topic/, thought/).
    if head in _DIR_TO_TYPE:
        node_type = _DIR_TO_TYPE[head]
    elif head in _TYPE_TO_DIR:
        node_type = head  # singular shortcut
    else:
        raise PathError(
            f"unknown subdir {head!r}; expected one of "
            f"papers/, topics/standalone-note/, topics/agent-created/, "
            f"thoughts/ (or singular shortcuts paper/, note/, topic/, "
            f"thought/)."
        )

    if not tail:
        raise PathError(f"{target!r} has no key after subdir")

    if ".." in tail.split("/"):
        raise PathError(f"{target!r}: '..' not allowed in key")

    return NodeAddress(node_type=node_type, key=tail)


def from_md_path(kb_root: Path, md_path: Path) -> NodeAddress:
    """Reverse: given an absolute md path inside kb_root, derive the
    NodeAddress. Raises PathError if the path isn't recognizable.
    """
    try:
        rel = md_path.resolve().relative_to(kb_root.resolve())
    except ValueError:
        raise PathError(f"{md_path} is outside kb_root {kb_root}")

    parts = rel.parts
    if len(parts) < 2:
        raise PathError(f"{rel} has no subdir")

    # Try two-segment match first (topics/standalone-note/,
    # topics/agent-created/).
    if len(parts) >= 3 and parts[0] == "topics" and parts[1] in ("standalone-note", "agent-created"):
        two_seg = f"{parts[0]}/{parts[1]}"
        node_type = _DIR_TO_TYPE[two_seg]
        tail = "/".join(parts[2:])
    else:
        subdir = parts[0]
        if subdir not in _DIR_TO_TYPE:
            # Detect deprecated v25 locations for better error messages.
            if subdir == "zotero-notes":
                raise PathError(
                    f"{rel}: 'zotero-notes/' is DEPRECATED in v26; "
                    f"move to 'topics/standalone-note/'."
                )
            if subdir == "topics":
                # topics/<slug>.md with no sub-bucket — legacy.
                raise PathError(
                    f"{rel}: top-level 'topics/<slug>.md' is "
                    f"DEPRECATED in v26; move to "
                    f"'topics/agent-created/<slug>.md'."
                )
            raise PathError(f"{rel} is not a known KB subdir")
        node_type = _DIR_TO_TYPE[subdir]
        tail = "/".join(parts[1:])

    # Key is everything after the subdir, minus the .md suffix.
    if tail.endswith(".md"):
        tail = tail[:-3]
    return NodeAddress(node_type=node_type, key=tail)
