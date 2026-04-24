"""kb-mcp config must reject non-mapping nested sections with a
clear ConfigError, not crash later with AttributeError.

Pre-fix, `raw.get("logging", {}) or {}` accepted bool/str/list
silently and the next `.get(...)` call raised AttributeError deep
in load_config — useless for debugging a YAML typo.
"""
from __future__ import annotations

import pytest

from kb_mcp.config import ConfigError, _mapping_section


def test_absent_section_returns_empty_dict():
    assert _mapping_section({}, "logging") == {}


def test_none_section_returns_empty_dict():
    """YAML `logging:` (key with no value) parses to None."""
    assert _mapping_section({"logging": None}, "logging") == {}


def test_valid_mapping_returns_unchanged():
    raw = {"logging": {"level": "debug"}}
    assert _mapping_section(raw, "logging") == {"level": "debug"}


def test_bool_section_raises():
    """`embeddings: false` is the common mistake: user thinks they're
    disabling embeddings but hands the loader a bool. The intended
    spelling is `embeddings:\\n  enabled: false`."""
    with pytest.raises(ConfigError) as exc:
        _mapping_section({"embeddings": False}, "embeddings")
    msg = str(exc.value)
    assert "embeddings" in msg
    assert "mapping" in msg.lower()
    assert "bool" in msg
    assert "indentation" in msg.lower()


def test_string_section_raises():
    """`logging: debug` — user flattened what should be a mapping
    into a scalar. _validate_log_level used to swallow this but we
    now intercept earlier."""
    with pytest.raises(ConfigError) as exc:
        _mapping_section({"logging": "debug"}, "logging")
    msg = str(exc.value)
    assert "logging" in msg
    assert "str" in msg


def test_list_section_raises():
    """`store:\\n  - journal_mode: wal` parses as a list-of-mappings,
    not a mapping. YAML indentation error — catch it."""
    with pytest.raises(ConfigError) as exc:
        _mapping_section(
            {"store": [{"journal_mode": "wal"}]},
            "store",
        )
    msg = str(exc.value)
    assert "store" in msg
    assert "list" in msg


def test_int_section_raises():
    with pytest.raises(ConfigError) as exc:
        _mapping_section({"embeddings": 42}, "embeddings")
    assert "int" in str(exc.value)


def test_load_config_surfaces_bool_section_as_configerror(tmp_path):
    """End-to-end: a config file with `embeddings: false` triggers
    ConfigError at load time, not AttributeError later."""
    from kb_mcp.config import load_config

    cfg_path = tmp_path / "kb-mcp.yaml"
    cfg_path.write_text(
        "kb_root: " + str(tmp_path) + "\n"
        "embeddings: false\n",
        encoding="utf-8",
    )
    # kb_root of tmp_path → no ee-kb subdir, but load_config will
    # still hit the section check first because YAML parsing + kb_root
    # resolution happen before section reads.
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=cfg_path, kb_root=tmp_path)
    assert "embeddings" in str(exc.value)
    assert "mapping" in str(exc.value).lower()
