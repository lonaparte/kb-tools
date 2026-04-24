"""Config loading for kb-mcp.

**Configuration storage policy (strict):**

All config files live in `<workspace>/.ee-kb-tools/config/`. Nothing
under `~/.config/`, `/etc/`, or any other system path. Only API keys
read from environment variables (meant to live in the user's shell
rc).

Config file: `<workspace>/.ee-kb-tools/config/kb-mcp.yaml`.
File is optional — all fields have sane defaults.

Resolution order for config file path:
  1. `--config` CLI arg (explicit override)
  2. `$KB_MCP_CONFIG` env var (scripting / testing override)
  3. `<workspace>/.ee-kb-tools/config/kb-mcp.yaml` (canonical)

Resolution order for kb_root (knowledge base path):
  1. `--kb-root` CLI arg
  2. `$KB_ROOT` env var
  3. `kb_root:` key in the config file
  4. Workspace autodetect (find `.ee-kb-tools/` ancestor, look for
     sibling `ee-kb/`)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


def _parse_bool(value, *, default: bool, field: str) -> bool:
    """Coerce a YAML-parsed config value into a strict bool.

    Never use `bool(value)` for this — it's a notorious Python footgun:
    `bool("false")` returns True, `bool("0")` returns True, and so on.
    Users who write `enabled: "false"` (quoting the value by habit, or
    because their YAML editor auto-quoted it) would silently get the
    OPPOSITE behaviour from what they typed. Since this particular
    flag gates API calls and cost, the failure mode is expensive.

    Accepted shapes:
      - Python bool True/False (YAML unquoted `true`/`false`/`yes`/`no`
        are parsed by PyYAML as bool already).
      - Python int 0 / 1.
      - String "true"/"false"/"yes"/"no"/"on"/"off"/"1"/"0" (any case,
        surrounding whitespace stripped).

    Anything else raises ConfigError with a clear message listing the
    accepted forms. None / missing key falls back to `default`.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # 0/1 only; 2 should not silently become True.
        if value in (0, 1):
            return bool(value)
        raise ConfigError(
            f"config field {field!r}: integer value {value} is not a "
            f"valid bool. Use true/false, yes/no, on/off, or 0/1."
        )
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0"):
            return False
        raise ConfigError(
            f"config field {field!r}: string value {value!r} is not a "
            f"valid bool. Use true/false, yes/no, on/off, or 0/1."
        )
    raise ConfigError(
        f"config field {field!r}: value of type {type(value).__name__} "
        f"is not a valid bool. Use true/false, yes/no, on/off, or 0/1."
    )


def _parse_positive_int(value, *, field: str) -> int:
    """Coerce config value to a positive int. Rejects 0, negatives,
    non-integer strings, and floats (even "integer-valued" ones like
    1.0, because YAML `1.5` would silently round to `1` under int(),
    which is surprising enough to warrant an explicit error). Used
    for fields like `embeddings.batch_size` where 0 would cause
    range(start, stop, 0) to raise ValueError deep inside the indexer,
    and a negative value would produce an empty loop (silently
    processing nothing).
    """
    if isinstance(value, bool):
        # bool is int subclass in Python; explicitly reject to avoid
        # `True` being accepted as 1 in a numeric field (almost
        # certainly a config typo if it happens).
        raise ConfigError(
            f"config field {field!r}: bool {value!r} is not a valid "
            f"positive integer"
        )
    if isinstance(value, float):
        # YAML `1.5` parses to float; int(1.5) silently drops the
        # fraction. Reject so the user sees their typo.
        raise ConfigError(
            f"config field {field!r}: float {value!r} is not a valid "
            f"integer (use an integer literal)"
        )
    try:
        iv = int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"config field {field!r}: {value!r} is not an integer ({e})"
        ) from None
    if iv <= 0:
        raise ConfigError(
            f"config field {field!r}: {iv} is not a positive integer "
            f"(must be >= 1)"
        )
    return iv


@dataclass
class Config:
    kb_root: Path
    zotero_root: Path | None = None
    log_level: str = "info"
    log_file: Path | None = None

    # Embedding configuration. All optional — if the API key env var
    # isn't set, embedding is skipped and kb-mcp falls back to
    # FTS5-only search.
    embeddings_enabled: bool = True
    # Provider: "openai" | "gemini" | "openrouter"
    embedding_provider: str = "openai"
    embedding_model: str | None = None
    embedding_dim: int | None = None
    # Env var names. Customize these only if you have multiple keys
    # and need to distinguish them.
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_base_url: str | None = None
    gemini_api_key_env: str = "GEMINI_API_KEY"
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_base_url: str | None = None  # default resolves in the provider
    embedding_batch_size: int = 100

    # SQLite journal mode. "delete" (default) keeps `.kb-mcp/` as a
    # single self-contained file (portable via rsync/Syncthing).
    # "wal" adds .sqlite-wal + .sqlite-shm sidecars but gives better
    # concurrent-read tolerance during long writes. Change only if
    # you run a persistent MCP server alongside frequent re-indexes.
    journal_mode: str = "delete"


def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser().resolve()


def _resolve_embedding_model(provider: str, model: str | None) -> str:
    if model:
        return model
    if provider == "openai":
        return "text-embedding-3-small"
    if provider == "gemini":
        return "gemini-embedding-001"
    if provider == "openrouter":
        # Default: route to OpenAI's text-embedding-3-small via
        # OpenRouter. Same underlying model as provider="openai" but
        # billed through OpenRouter — useful when a user already
        # has an OPENROUTER_API_KEY but not an OPENAI_API_KEY.
        return "openai/text-embedding-3-small"
    raise ConfigError(f"unknown embedding_provider {provider!r}")


def _find_workspace_config() -> Path | None:
    """Locate `kb-mcp.yaml` in the canonical `<parent>/.ee-kb-tools/
    config/` location. Returns None if workspace can't be resolved or
    the file doesn't exist (both are OK — callers can run on defaults).
    """
    try:
        from .workspace import resolve_workspace
        ws = resolve_workspace()
    except Exception:
        return None
    candidate = ws.kb_mcp_config()
    return candidate if candidate.exists() else None


def load_config(
    config_path: Path | None = None,
    kb_root: Path | None = None,
) -> Config:
    """Load kb-mcp config per the resolution order above.

    `config_path` and `kb_root` are explicit overrides that skip
    env/workspace resolution for those specific values.
    """
    # 1. Pick config file.
    if config_path is None:
        env_cfg = os.environ.get("KB_MCP_CONFIG")
        if env_cfg:
            config_path = Path(env_cfg).expanduser()
        else:
            config_path = _find_workspace_config()

    raw: dict = {}
    if config_path and config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        # Guard against YAML where the top-level isn't a mapping
        # (e.g. someone put a list at the root, or accidentally wrote
        # `- key: value` instead of `key: value`). raw.get(...) below
        # would otherwise crash with a confusing AttributeError.
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

    # 2. Pick kb_root.
    kr = kb_root or os.environ.get("KB_ROOT") or raw.get("kb_root")
    zotero_r = os.environ.get("ZOTERO_ROOT") or raw.get("zotero_root")

    if not kr:
        try:
            from .workspace import resolve_workspace
            ws = resolve_workspace()
            kr = str(ws.kb_root)
            if not zotero_r and ws.zotero_root.exists():
                zotero_r = str(ws.zotero_root)
        except Exception as e:
            raise ConfigError(
                "kb_root is required. Set via --kb-root, KB_ROOT env "
                "var, kb_root in config file, or use the canonical "
                "workspace layout (.ee-kb-tools/ + ee-kb/ siblings).\n"
                f"Autodetect error: {e}"
            ) from e

    log_cfg = raw.get("logging", {}) or {}
    log_file = log_cfg.get("file")

    emb_cfg = raw.get("embeddings", {}) or {}
    provider = str(emb_cfg.get("provider", "openai")).lower()

    store_cfg = raw.get("store", {}) or {}
    journal_mode = str(store_cfg.get("journal_mode", "delete")).lower()

    return Config(
        kb_root=_expand(kr),
        zotero_root=_expand(zotero_r) if zotero_r else None,
        log_level=log_cfg.get("level", "info"),
        log_file=_expand(log_file) if log_file else None,
        embeddings_enabled=_parse_bool(
            emb_cfg.get("enabled"), default=True,
            field="embeddings.enabled",
        ),
        embedding_provider=provider,
        embedding_model=_resolve_embedding_model(provider, emb_cfg.get("model")),
        embedding_dim=emb_cfg.get("dim"),
        openai_api_key_env=str(emb_cfg.get("openai_api_key_env", "OPENAI_API_KEY")),
        openai_base_url=emb_cfg.get("openai_base_url"),
        gemini_api_key_env=str(emb_cfg.get("gemini_api_key_env", "GEMINI_API_KEY")),
        openrouter_api_key_env=str(
            emb_cfg.get("openrouter_api_key_env", "OPENROUTER_API_KEY")
        ),
        openrouter_base_url=emb_cfg.get("openrouter_base_url"),
        embedding_batch_size=_parse_positive_int(
            emb_cfg.get("batch_size", 100),
            field="embeddings.batch_size",
        ),
        journal_mode=journal_mode,
    )
