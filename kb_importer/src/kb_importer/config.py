"""Configuration loading for kb-importer.

**Configuration storage policy (strict):**

All config files live in `<workspace>/.ee-kb-tools/config/`. Nothing
under `~/.config/`, `/etc/`, or any other system path. Only API keys
read from environment variables (meant to live in the user's shell
rc).

Config file: `<workspace>/.ee-kb-tools/config/kb-importer.yaml`.
File is optional — but typically required to set Zotero library_id.

Resolution order for config file path:
  1. `--config` CLI arg (explicit override)
  2. `$KB_IMPORTER_CONFIG` env var (scripting / testing override)
  3. `<workspace>/.ee-kb-tools/config/kb-importer.yaml` (canonical,
     autodetected via `.ee-kb-tools/` sibling)

Required fields: zotero_storage_dir, kb_root. Each can come from CLI
args, env vars, or the config file.

## Two source modes (metadata origin)

kb-importer reads item metadata from Zotero in one of two modes:

- **web** (default, v0.28.0+): Zotero's cloud web API
  (api.zotero.org). Requires a `library_id`, an API key (via env
  var), and network access. Does NOT require Zotero to be
  running anywhere. Works uniformly from headless servers and
  laptops alike — the most portable setup, which is why it's now
  the default.

- **live**: Zotero's local HTTP API at localhost:23119. Requires
  Zotero to be running on the same machine. No network needed.
  Pre-0.28.0 default. Still supported — set
  `source_mode: live` in config or pass `--zotero-source live`
  to get it.

Both modes use the same local `zotero_storage_dir` to find PDFs. The
assumption: PDFs are the same regardless of metadata source (same item
keys everywhere). For a server that doesn't have a Zotero install, you
can rsync your `~/Zotero/storage/` to it and point `zotero_storage_dir`
at that copy.

## TODO

A future "sqlite" mode could read Zotero's `zotero.sqlite` directly,
for a fully offline snapshot. Not yet implemented — the Zotero SQLite
schema is not a stable API.
"""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml


VALID_SOURCE_MODES = ("live", "web")
DEFAULT_API_KEY_ENV = "ZOTERO_API_KEY"


def _find_workspace_config() -> Path | None:
    """Locate `kb-importer.yaml` in the canonical
    `<parent>/.ee-kb-tools/config/` location.

    v0.29.4: now tries TWO autodetect paths, in order:

      1. `find_tools_dir()` — walks up from this module's install
         location looking for `.ee-kb-tools`. Works when the code
         is deployed inside `.ee-kb-tools/kb_importer/...` (the
         `scripts/deploy.sh` layout). Returns None for any other
         install location (site-packages, editable-install from
         ~/dev/, etc).

      2. `find_workspace_root()` — walks up from CWD looking for
         a dir that contains `.ee-kb-tools/` (or `ee-kb/`). This
         is the user-facing path: `cd workspace/ee-kb && kb-importer
         status` resolves its config even when the code is in a
         site-packages wheel install far from the workspace.

    Pre-0.29.4, only (1) ran. So anyone with a pip-wheel install +
    a workspace elsewhere on disk would have config autodetect
    silently return None despite having a correctly-placed
    `.ee-kb-tools/config/kb-importer.yaml`. The same bug existed
    in kb_citations; both fixed here.

    Caller falls back to CLI args / env vars if both autodetect
    paths fail.
    """
    from kb_core.workspace import (
        find_tools_dir, find_workspace_root, TOOLS_DIR_NAME,
    )

    # (1) code-install-based.
    tools = find_tools_dir()
    if tools is not None:
        candidate = tools / "config" / "kb-importer.yaml"
        if candidate.exists():
            return candidate

    # (2) CWD-based walk-up.
    ws = find_workspace_root()
    if ws is not None:
        candidate = ws / TOOLS_DIR_NAME / "config" / "kb-importer.yaml"
        if candidate.exists():
            return candidate

    return None


@dataclass
class Config:
    """Runtime configuration for kb-importer."""

    zotero_storage_dir: Path
    kb_root: Path

    # Zotero source selection
    zotero_source_mode: str = "web"           # v0.28.0: default flipped live → web
    zotero_library_id: str = ""               # required iff mode == "web"
    zotero_library_type: str = "user"         # "user" | "group"
    zotero_api_key_env: str = DEFAULT_API_KEY_ENV  # env var name; value read at runtime

    log_level: str = "info"
    log_file: Path | None = None
    # Reserved for future --fulltext support (not used in MVP).
    fulltext: dict = field(default_factory=dict)

    @property
    def papers_dir(self) -> Path:
        return self.kb_root / "papers"

    @property
    def notes_dir(self) -> Path:
        # v26: standalone Zotero notes live under topics/standalone-note/
        # (was zotero-notes/ in v25).
        return self.kb_root / "topics" / "standalone-note"

    @property
    def storage_dir(self) -> Path:
        """Alias kept for internal call sites; points to the per-item
        storage root (the directory that contains {zotero_key}/ subdirs).
        """
        return self.zotero_storage_dir

    # 0.29.1: `archive_dir` property removed. The `_archived/` feature
    # was fully deleted; no code needs to know about a sibling dir
    # under storage anymore.


def _expand(p: str | Path) -> Path:
    """Expand ~ and env vars, return absolute Path."""
    return Path(os.path.expandvars(str(p))).expanduser().resolve()


def load_config(
    config_path: Path | None = None,
    zotero_storage_dir: Path | None = None,
    kb_root: Path | None = None,
    zotero_source_mode: str | None = None,
    zotero_library_id: str | None = None,
) -> Config:
    """Load config. Precedence: CLI args > env vars > file > defaults.

    Back-compat: legacy key `zotero_mirror` / KB_ZOTERO_MIRROR still
    accepted, with a DeprecationWarning.
    """
    # 1. Locate config file.
    if config_path is None:
        env_cfg = os.environ.get("KB_IMPORTER_CONFIG")
        if env_cfg:
            config_path = Path(env_cfg).expanduser()
        else:
            # Autodetect via workspace layout. If no workspace or no
            # config file there, `config_path` ends up None and we
            # proceed with CLI-arg / env-var only resolution.
            config_path = _find_workspace_config()

    # 2. Load file if it exists.
    raw: dict = {}
    if config_path and config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        if loaded is None:
            raw = {}
        elif isinstance(loaded, dict):
            raw = loaded
        else:
            raise ConfigError(
                f"config file {config_path} has a {type(loaded).__name__} "
                f"at the top level, but a mapping (key: value pairs) is "
                f"required. Check the YAML indentation."
            )

    zotero_block = raw.get("zotero") or {}

    # 3. Resolve zotero_storage_dir, with legacy-key fallback.
    storage_value = (
        zotero_storage_dir
        or os.environ.get("KB_ZOTERO_STORAGE")
        or raw.get("zotero_storage_dir")
    )
    if not storage_value:
        legacy = os.environ.get("KB_ZOTERO_MIRROR") or raw.get("zotero_mirror")
        if legacy:
            warnings.warn(
                "Config key `zotero_mirror` / KB_ZOTERO_MIRROR is deprecated. "
                "Use `zotero_storage_dir` / KB_ZOTERO_STORAGE instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            legacy_path = _expand(legacy)
            candidate = legacy_path / "storage"
            storage_value = candidate if candidate.exists() else legacy_path

    # 4. Resolve kb_root.
    kr = kb_root or os.environ.get("KB_ROOT") or raw.get("kb_root")

    # 4b. Last-resort: workspace autodetect for both kb_root and
    #     zotero_storage_dir. Fills whichever is still missing.
    #     Walks up for `.ee-kb-tools/`, then reads sibling directories.
    #     Does not depend on kb_write (standalone-friendly).
    if not kr or not storage_value:
        here = Path(__file__).resolve()
        for p in [here] + list(here.parents):
            if p.name == ".ee-kb-tools":
                ws_parent = p.parent
                if not kr:
                    kb_candidate = ws_parent / "ee-kb"
                    if kb_candidate.exists():
                        kr = str(kb_candidate)
                if not storage_value:
                    storage_candidate = ws_parent / "zotero" / "storage"
                    if storage_candidate.exists():
                        storage_value = str(storage_candidate)
                break

    # 5. Resolve Zotero source mode.
    mode = (
        zotero_source_mode
        or os.environ.get("KB_ZOTERO_SOURCE")
        or zotero_block.get("source_mode")
        or "web"
    )
    mode = mode.lower().strip()
    if mode not in VALID_SOURCE_MODES:
        raise ConfigError(
            f"Invalid zotero source mode {mode!r}. "
            f"Expected one of: {', '.join(VALID_SOURCE_MODES)}."
        )

    # 6. Resolve library_id / library_type / api_key_env (only needed for web).
    lib_id = (
        zotero_library_id
        or os.environ.get("ZOTERO_LIBRARY_ID")
        or zotero_block.get("library_id")
        or ""
    )
    lib_type = (
        os.environ.get("ZOTERO_LIBRARY_TYPE")
        or zotero_block.get("library_type")
        or "user"
    )
    api_key_env = (
        os.environ.get("KB_ZOTERO_API_KEY_ENV")
        or zotero_block.get("api_key_env")
        or DEFAULT_API_KEY_ENV
    )

    # 7. Validation.
    if not storage_value:
        raise ConfigError(
            "zotero_storage_dir is required. Set via --zotero-storage, "
            "KB_ZOTERO_STORAGE env var, or `zotero_storage_dir` in the "
            "config file. Typically ~/Zotero/storage."
        )
    if not kr:
        raise ConfigError(
            "kb_root is required. Set via --kb-root, KB_ROOT env var, "
            "or `kb_root` in the config file."
        )
    if mode == "web":
        if not lib_id:
            raise ConfigError(
                "Web mode requires a library_id. Set via --zotero-library-id, "
                "ZOTERO_LIBRARY_ID env var, or `zotero.library_id` in config. "
                "Find yours at https://www.zotero.org/settings/keys (shown as "
                "'Your userID for use in API calls')."
            )
        if lib_type not in ("user", "group"):
            raise ConfigError(
                f"zotero.library_type must be 'user' or 'group', got {lib_type!r}."
            )
        # We don't validate that the env var is set here — that's done
        # by ZoteroReader when it actually tries to connect. This lets
        # `status` and `list --config-only` work without requiring the
        # key to be present.

    log_cfg = raw.get("logging", {}) or {}
    log_file = log_cfg.get("file")
    return Config(
        zotero_storage_dir=_expand(storage_value),
        kb_root=_expand(kr),
        zotero_source_mode=mode,
        zotero_library_id=str(lib_id),
        zotero_library_type=lib_type,
        zotero_api_key_env=api_key_env,
        log_level=log_cfg.get("level", "info"),
        log_file=_expand(log_file) if log_file else None,
        fulltext=raw.get("fulltext", {}) or {},
    )


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""
