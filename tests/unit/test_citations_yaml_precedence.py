"""1.4.5: regression for the kb-citations YAML/CLI default-shadow bug.

Pre-1.4.5, `--max-refs`, `--max-cites`, and `--freshness-days` had
literal argparse defaults (1000 / 200 / 30) that silently overrode
the user's `kb-citations.yaml` config when the CLI flag was not
passed. `--with-citations` was `action="store_true"`, so YAML
`fetch_citations: true` could not be turned back off from the CLI.

These tests pin the post-fix precedence:
    explicit CLI flag > YAML config > built-in fallback
and the special-case `freshness_days: 0 → "force refetch"` semantic.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from kb_citations.cli import _parser, _build_ctx


def _parse(argv):
    return _parser().parse_args(argv)


def _yaml_config(monkeypatch, tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "kb-citations.yaml"
    cfg.write_text(body, encoding="utf-8")
    # Both the workspace lookup AND a possible kb_root resolution
    # need to see this temp dir as the workspace. The stdlib test
    # runner's monkeypatch only supports the 3-arg form
    # (module_obj, "attr", value); use that for cross-runner safety.
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    import kb_citations.cli as kc_cli
    monkeypatch.setattr(kc_cli, "find_workspace_config", lambda: cfg)
    monkeypatch.setattr(kc_cli, "kb_root_from_env", lambda _x: tmp_path)
    return cfg


def test_yaml_config_applied_when_cli_flag_omitted(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, """\
max_refs: 200
max_cites: 50
freshness_days: 7
fetch_citations: true
""")
    args = _parse(["fetch"])
    ctx = _build_ctx(args)
    assert ctx.max_refs == 200
    assert ctx.max_cites == 50
    assert ctx.freshness_days == 7
    assert ctx.fetch_citations is True


def test_cli_flag_overrides_yaml(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, """\
max_refs: 200
max_cites: 50
freshness_days: 7
""")
    args = _parse([
        "fetch", "--max-refs", "9", "--max-cites", "8", "--freshness-days", "6",
    ])
    ctx = _build_ctx(args)
    assert ctx.max_refs == 9
    assert ctx.max_cites == 8
    assert ctx.freshness_days == 6


def test_freshness_days_zero_forces_refetch(monkeypatch, tmp_path):
    """freshness_days=0 (CLI or YAML) must surface as None — meaning
    'don't skip anything based on cache age'. Pre-1.4.5 the CLI
    default 30 silently shadowed YAML's 0; post-fix, 0 propagates."""
    _yaml_config(monkeypatch, tmp_path, "freshness_days: 0\n")
    ctx = _build_ctx(_parse(["fetch"]))
    assert ctx.freshness_days is None

    # Same via CLI:
    _yaml_config(monkeypatch, tmp_path, "")
    ctx = _build_ctx(_parse(["fetch", "--freshness-days", "0"]))
    assert ctx.freshness_days is None


def test_no_with_citations_overrides_yaml_true(monkeypatch, tmp_path):
    """`--no-with-citations` must turn off a YAML-enabled
    fetch_citations. Pre-1.4.5 this was impossible (store_true)."""
    _yaml_config(monkeypatch, tmp_path, "fetch_citations: true\n")
    ctx = _build_ctx(_parse(["fetch", "--no-with-citations"]))
    assert ctx.fetch_citations is False


def test_with_citations_overrides_yaml_false(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, "fetch_citations: false\n")
    ctx = _build_ctx(_parse(["fetch", "--with-citations"]))
    assert ctx.fetch_citations is True


def test_builtin_fallbacks_when_neither_cli_nor_yaml(monkeypatch, tmp_path):
    """No YAML, no CLI → fall back to the documented builtin
    defaults (1000 / 200 / 30 / False)."""
    _yaml_config(monkeypatch, tmp_path, "")
    ctx = _build_ctx(_parse(["fetch"]))
    assert ctx.max_refs == 1000
    assert ctx.max_cites == 200
    assert ctx.freshness_days == 30
    assert ctx.fetch_citations is False


def test_yaml_partial_keys_use_builtin_for_missing(monkeypatch, tmp_path):
    """YAML setting only some keys → others use the builtin default,
    not whatever happened to be in the file's neighborhood."""
    _yaml_config(monkeypatch, tmp_path, "max_refs: 500\n")
    ctx = _build_ctx(_parse(["fetch"]))
    assert ctx.max_refs == 500           # from YAML
    assert ctx.max_cites == 200          # builtin
    assert ctx.freshness_days == 30      # builtin
    assert ctx.fetch_citations is False  # builtin


def test_yaml_non_dict_top_level_ignored(monkeypatch, tmp_path):
    """A YAML file that parses to a list / scalar instead of a
    mapping must not crash _build_ctx. Warning is printed (we don't
    assert the warning text — both runners would need a capture
    fixture), builtin defaults applied."""
    _yaml_config(monkeypatch, tmp_path, "- foo\n- bar\n")
    ctx = _build_ctx(_parse(["fetch"]))
    # Builtins regardless of the malformed top level.
    assert ctx.max_refs == 1000
    assert ctx.max_cites == 200
    assert ctx.freshness_days == 30


def test_yaml_malformed_does_not_crash(monkeypatch, tmp_path):
    """Genuinely broken YAML: warn + fall back, don't propagate the
    parse error to the user. Same shape as the existing 'could not
    read' branch in _build_ctx."""
    # Unmatched bracket — yaml.safe_load raises.
    _yaml_config(monkeypatch, tmp_path, "max_refs: [1, 2,\n")
    ctx = _build_ctx(_parse(["fetch"]))
    # Falls back to builtins.
    assert ctx.max_refs == 1000


def test_unknown_yaml_keys_ignored(monkeypatch, tmp_path):
    """Stray / future-version YAML keys must not crash the merge —
    they're just unused."""
    _yaml_config(monkeypatch, tmp_path, """\
max_refs: 42
totally_made_up_key: hello
nested:
  also_unknown: 7
""")
    ctx = _build_ctx(_parse(["fetch"]))
    assert ctx.max_refs == 42
