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
    """Walk up from this module's install location looking for
    `.ee-kb-tools`. Returns the directory or None. Standalone —
    does not depend on kb_write.
    """
    here = Path(__file__).resolve()
    for p in [here] + list(here.parents):
        if p.name == ".ee-kb-tools":
            return p
    return None


def find_workspace_config() -> Path | None:
    """Canonical location for kb-citations YAML:
    `<parent>/.ee-kb-tools/config/kb-citations.yaml`. Returns the
    Path if it exists, else None.
    """
    tools = _find_tools_dir()
    if tools is None:
        return None
    candidate = tools / "config" / "kb-citations.yaml"
    return candidate if candidate.exists() else None


def kb_root_from_env(explicit: Path | None = None) -> Path:
    """Resolve kb_root. Precedence: explicit > $KB_ROOT > workspace
    autodetect (sibling `ee-kb/` of `.ee-kb-tools/`). Never reads
    system paths.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("KB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # Workspace autodetect.
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
