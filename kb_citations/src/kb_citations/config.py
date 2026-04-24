"""Config for kb_citations.

**Configuration storage policy (strict):**

All config comes from (in precedence order):
  1. CLI arg / constructor arg (explicit override)
  2. Environment variable
  3. `<workspace>/.ee-kb-tools/config/kb-citations.yaml` — autodetected
  4. Workspace sibling layout for kb_root

Nothing is read from `~/.config/`, `~/.local/share/`, `/etc/`, or
any other system path. API keys (if any) come from env vars and
are never stored in files.

Workspace autodetect: walks up from this module's install location
looking for a `.ee-kb-tools/` ancestor. If found, its sibling
`ee-kb/` is the kb_root and its `config/kb-citations.yaml` is the
config file. Does not depend on kb_write — kb_citations can be
installed standalone.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CitationsContext:
    """Knobs for a fetch or link run.

    kb_root: required.
    provider: 'semantic_scholar' or 'openalex'.
    api_key: S2 API key (optional but recommended).
    mailto: contact email (required for OpenAlex).
    max_refs / max_cites: per-paper caps.
    freshness_days: skip papers whose cache is newer than this.
        None = always refetch; default 30 days.
    fetch_citations: also fetch incoming citations (not just
        outgoing references). Doubles fetch cost but valuable for
        bridge/foundation analyses.
    """
    kb_root: Path
    provider: str = "semantic_scholar"
    api_key: str | None = None
    mailto: str | None = None
    max_refs: int = 1000
    max_cites: int = 200
    freshness_days: int | None = 30
    fetch_citations: bool = False

    def __post_init__(self):
        self.kb_root = Path(self.kb_root).expanduser().resolve()


def _find_tools_dir() -> Path | None:
    """v0.27.3: delegates to kb_core.workspace.find_tools_dir so all
    packages share one implementation. Prior to this, kb_citations,
    kb_importer, kb_mcp, and kb_write each had their own copy — a
    drift risk flagged by the v0.27 audit but fixed only for
    kb_mcp and kb_write in that pass.
    """
    from kb_core.workspace import find_tools_dir
    return find_tools_dir()


def find_workspace_config() -> Path | None:
    """Canonical location for kb-citations YAML:
    `<parent>/.ee-kb-tools/config/kb-citations.yaml`. Returns the
    Path if it exists, else None.

    CWD-first, install-location fallback. 0.29.5 re-ordered this
    (0.29.4 tried install first and fell back to CWD) so that an
    editable install in one workspace doesn't steal the config
    from a user whose CWD points to a different workspace.
    """
    from kb_core.workspace import find_workspace_root, TOOLS_DIR_NAME

    # (1) CWD-based walk-up — authoritative.
    ws = find_workspace_root()
    if ws is not None:
        candidate = ws / TOOLS_DIR_NAME / "config" / "kb-citations.yaml"
        if candidate.exists():
            return candidate

    # (2) code-install-based — deploy.sh compatibility.
    tools = _find_tools_dir()
    if tools is not None:
        candidate = tools / "config" / "kb-citations.yaml"
        if candidate.exists():
            return candidate

    return None


def kb_root_from_env(explicit: Path | None = None) -> Path:
    """Resolve kb_root. Precedence: explicit > $KB_ROOT > workspace
    autodetect (sibling `ee-kb/` of `.ee-kb-tools/`). Never reads
    system paths.

    Autodetect order: CWD-based walk first, then install-location
    fallback. Same rationale as `find_workspace_config` above.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("KB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    from kb_core.workspace import find_workspace_root

    # (1) CWD-based — authoritative.
    ws = find_workspace_root()
    if ws is not None:
        candidate = ws / "ee-kb"
        if candidate.exists():
            return candidate.resolve()

    # (2) install-location — deploy.sh compatibility.
    tools = _find_tools_dir()
    if tools is not None:
        candidate = tools.parent / "ee-kb"
        if candidate.exists():
            return candidate.resolve()

    raise ValueError(
        "kb_root not set. Provide via --kb-root, $KB_ROOT env var, "
        "or use the canonical workspace layout (.ee-kb-tools/ + "
        "ee-kb/ siblings)."
    )
