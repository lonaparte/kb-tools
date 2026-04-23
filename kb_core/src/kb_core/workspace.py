"""Workspace layout resolution and root-lookup helpers.

The canonical layout is three sibling directories under any parent:

    <parent>/
    ├── .ee-kb-tools/    # the tools (this package lives here)
    ├── ee-kb/           # the knowledge base
    └── zotero/
        └── storage/     # Zotero attachment store

The parent directory name is not fixed — it may be `workspace`,
`research`, `docs`, anything. What matters is the **sibling
relationship** of the three directories.

Resolution precedence for each path:
  1. Explicit arg (--kb-root, --zotero-root)
  2. Environment variable (KB_ROOT, ZOTERO_ROOT, KB_WORKSPACE)
  3. Autodetect from tools install location
  4. Error

Autodetect logic: find which directory contains this module's
caller (via find_tools_dir), go to its parent, and look for
`ee-kb/` and `zotero/` siblings.

Previously lived as near-duplicate copies in kb_mcp.workspace and
kb_write.workspace held in sync by a lint. In v27 this is the
canonical home; both packages now re-export it as a shim.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


TOOLS_DIR_NAME = ".ee-kb-tools"
KB_DIR_NAME = "ee-kb"
ZOTERO_DIR_NAME = "zotero"
ZOTERO_STORAGE_SUBDIR = "storage"


class WorkspaceError(Exception):
    """Raised when workspace layout can't be resolved."""


@dataclass(frozen=True)
class Workspace:
    """Resolved workspace paths. All absolute."""
    parent: Path
    tools_dir: Path        # <parent>/.ee-kb-tools
    config_dir: Path       # <parent>/.ee-kb-tools/config
    kb_root: Path          # <parent>/ee-kb
    zotero_root: Path      # <parent>/zotero
    zotero_storage: Path   # <parent>/zotero/storage

    def kb_mcp_config(self) -> Path:
        """Path to kb-mcp YAML config. May not exist; that's fine —
        all fields have defaults."""
        return self.config_dir / "kb-mcp.yaml"

    def kb_importer_config(self) -> Path:
        return self.config_dir / "kb-importer.yaml"

    def as_env(self) -> dict[str, str]:
        """Dict suitable for passing as env to child processes."""
        return {
            "KB_WORKSPACE": str(self.parent),
            "KB_ROOT": str(self.kb_root),
            "ZOTERO_ROOT": str(self.zotero_root),
            "ZOTERO_STORAGE": str(self.zotero_storage),
        }


def find_tools_dir() -> Path | None:
    """Walk up from THIS module's location looking for `.ee-kb-tools`.

    Works for:
      - code installed inside `.ee-kb-tools/` (the layout
        `scripts/deploy.sh` produces): file at
        .../.ee-kb-tools/kb_core/src/kb_core/workspace.py →
        returns .../.ee-kb-tools
      - code installed anywhere else (source checkout in
        ~/dev/kb-tools/, site-packages from a wheel, etc.):
        walks to filesystem root without finding `.ee-kb-tools/`
        and returns None.

    Returning None is not a failure — it just means the CLI
    should rely on CWD autodetect (`find_workspace_root`) or
    env vars instead. See `resolve_workspace` for the full
    precedence list.
    """
    here = Path(__file__).resolve()
    for p in [here] + list(here.parents):
        if p.name == TOOLS_DIR_NAME:
            return p
    return None


def find_workspace_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (defaults to CWD) looking for the KB
    parent — the directory that contains `ee-kb/`. Returns that
    parent, or None if none found.

    Recognises three shapes (in precedence order), matching what
    real users type:

      1. CWD is the parent itself (has an `ee-kb/` subdir). Most
         common case — user cd'd into their workspace.
      2. CWD is inside `ee-kb/` (one of its descendants). User
         navigated into papers or topics. We walk up until we
         find the dir whose parent has `ee-kb/`.
      3. CWD has a `.ee-kb-tools/` sibling-to-`ee-kb/` setup
         (the deployed layout). This is a subset of case 1 —
         `.ee-kb-tools/` sitting next to `ee-kb/` makes the
         parent identifiable either way, but accepting
         `.ee-kb-tools/` as a marker means the parent
         resolves even before `ee-kb/` exists (e.g. right
         after `deploy.sh` ran, before `kb-write init`).

    The code's own install location is NOT consulted here —
    that's `find_tools_dir`'s job, and only useful when the
    code lives under `.ee-kb-tools/`. Separating them means
    autodetect Just Works regardless of whether the source repo
    is inside the workspace, next to it, in `~/dev/`, or
    anywhere else on disk.
    """
    here = Path(start).resolve() if start is not None else Path.cwd().resolve()
    for p in [here] + list(here.parents):
        # Case 1 + case 3: CWD (or an ancestor) directly contains
        # `ee-kb/` or `.ee-kb-tools/`.
        if (p / KB_DIR_NAME).is_dir() or (p / TOOLS_DIR_NAME).is_dir():
            return p
        # Case 2: CWD is `ee-kb/` itself, or deeper inside it. The
        # directory named `ee-kb/` found on the walk IS the KB;
        # its parent is what we want.
        if p.name == KB_DIR_NAME and p.is_dir():
            return p.parent
    return None


def find_kb_root(start: Path | None = None) -> Path | None:
    """Convenience wrapper: find the workspace root and return its
    `ee-kb/` subdir if present. None otherwise.
    """
    ws = find_workspace_root(start)
    if ws is None:
        return None
    kb = ws / KB_DIR_NAME
    return kb if kb.is_dir() else None


def resolve_workspace(
    *,
    parent: Path | None = None,
    kb_root: Path | None = None,
    zotero_root: Path | None = None,
) -> Workspace:
    """Resolve workspace paths following the documented precedence.

    Any arg may be None; resolution fills missing pieces from env
    or autodetect.

    Raises WorkspaceError if no workable layout found.
    """
    # 1. Use explicit parent if given.
    if parent is not None:
        p = Path(parent).expanduser().resolve()
        return _workspace_from_parent(p, kb_root, zotero_root)

    # 2. $KB_WORKSPACE env var (explicitly points to the parent).
    env_parent = os.environ.get("KB_WORKSPACE")
    if env_parent:
        p = Path(env_parent).expanduser().resolve()
        return _workspace_from_parent(p, kb_root, zotero_root)

    # 3. Derive from $KB_ROOT (if user set only that, parent is its
    #    parent).
    env_kb = os.environ.get("KB_ROOT") or (str(kb_root) if kb_root else None)
    if env_kb:
        kb = Path(env_kb).expanduser().resolve()
        if kb.name == KB_DIR_NAME:
            return _workspace_from_parent(kb.parent, kb, zotero_root)
        # kb_root doesn't follow naming convention — accept it but
        # warn (can't auto-derive zotero_root sibling).
        return _workspace_custom(kb, zotero_root)

    # 4. Autodetect from CWD — walks up looking for a directory
    #    containing ee-kb/ (or .ee-kb-tools/, or being ee-kb/ itself).
    #    Handles the common "user cd'd to workspace / into KB and ran
    #    a command" flow without any env var.
    ws_root = find_workspace_root()
    if ws_root is not None:
        return _workspace_from_parent(ws_root, kb_root, zotero_root)

    # 5. Autodetect from code location — last resort, only hits when
    #    the code itself lives under .ee-kb-tools/ (deployed layout).
    #    When the code lives elsewhere (e.g. ~/dev/kb-tools/), this
    #    returns None and we fall through to the error below.
    tools = find_tools_dir()
    if tools is not None:
        return _workspace_from_parent(tools.parent, kb_root, zotero_root)

    raise WorkspaceError(
        "could not resolve workspace layout. Any ONE of:\n"
        "  • cd into the directory containing `ee-kb/` and retry\n"
        "  • export KB_ROOT=<path to your ee-kb directory>\n"
        "  • export KB_WORKSPACE=<parent dir containing ee-kb/>\n"
        "  • pass --kb-root <path> on the CLI"
    )


def _workspace_from_parent(
    parent: Path,
    kb_override: Path | None,
    zotero_override: Path | None,
) -> Workspace:
    tools_dir = parent / TOOLS_DIR_NAME
    config_dir = tools_dir / "config"
    kb = Path(kb_override).expanduser().resolve() if kb_override else parent / KB_DIR_NAME
    zotero = Path(zotero_override).expanduser().resolve() if zotero_override else parent / ZOTERO_DIR_NAME
    storage = zotero / ZOTERO_STORAGE_SUBDIR

    if not kb.exists():
        raise WorkspaceError(
            f"ee-kb directory not found at {kb}. Either create it or "
            "set KB_ROOT / --kb-root to the correct location."
        )
    return Workspace(
        parent=parent.resolve(),
        tools_dir=tools_dir,
        config_dir=config_dir,
        kb_root=kb,
        zotero_root=zotero,
        zotero_storage=storage,
    )


def _workspace_custom(
    kb: Path,
    zotero_override: Path | None,
) -> Workspace:
    """kb_root doesn't follow convention; fill zotero from env if given,
    otherwise leave as placeholder."""
    if not kb.exists():
        raise WorkspaceError(
            f"ee-kb directory not found at {kb}. Either create it or "
            "set KB_ROOT / --kb-root to the correct location."
        )
    zotero = (
        Path(zotero_override).expanduser().resolve() if zotero_override
        else Path(os.environ.get("ZOTERO_ROOT", str(kb.parent / ZOTERO_DIR_NAME))).expanduser().resolve()
    )
    tools_dir = kb.parent / TOOLS_DIR_NAME
    return Workspace(
        parent=kb.parent,
        tools_dir=tools_dir,
        config_dir=tools_dir / "config",
        kb_root=kb,
        zotero_root=zotero,
        zotero_storage=zotero / ZOTERO_STORAGE_SUBDIR,
    )
