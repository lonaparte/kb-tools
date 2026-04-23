"""Per-subcommand argparse + dispatch modules.

Each module exports `register(sub)` which wires its parser(s) onto
the shared `sub = argparse._SubParsersAction`. `cli.py` imports and
calls each in order. Helper functions used across modules live in
`_shared`.

Split in v0.28.0 from a single 1100-line `cli.py` to make argparse
wiring easier to navigate. The CLI surface is unchanged — this is
purely an internal refactor.
"""
from __future__ import annotations

from . import (
    init_cmd,
    node_cmd,
    pref_cmd,
    zone_cmd,
    field_cmd,
    admin_cmd,
    batch_cmd,
    migrate_cmd,
)


def register_all(sub) -> None:
    """Register every subcommand onto `sub`. Order matches the
    pre-split cli.py so `--help` output is stable."""
    init_cmd.register(sub)
    node_cmd.register_thought(sub)
    node_cmd.register_topic(sub)
    pref_cmd.register(sub)
    zone_cmd.register(sub)
    field_cmd.register_tag(sub)
    field_cmd.register_ref(sub)
    admin_cmd.register_delete(sub)
    admin_cmd.register_log(sub)
    admin_cmd.register_rules(sub)
    admin_cmd.register_doctor(sub)
    batch_cmd.register_re_summarize(sub)
    batch_cmd.register_re_read(sub)
    migrate_cmd.register_legacy_chapters(sub)
    migrate_cmd.register_slugs(sub)
