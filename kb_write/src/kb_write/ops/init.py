"""`kb-write init`: scaffold a KB with agent-discovery files.

Creates (or refreshes) these at kb_root:

  - README.md, CLAUDE.md, AGENTS.md — rendered from fragments
  - AGENT-WRITE-RULES.md            — copied verbatim (long doc)
  - .agent-prefs/README.md          — copied verbatim

Rendered files (README, CLAUDE, AGENTS) come from
`kb_write.prompts.renderer`, composing reusable fragments. Edit a
fragment → `kb-write init --refresh` → every agent entry file
updates. Single source of truth.

--refresh preserves any user content appended AFTER the generated
block marker. --force overwrites everything without preservation.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..atomic import atomic_write
from ..prompts import renderer as prompt_renderer


# Non-rendered scaffold files — copied verbatim.
STATIC_SCAFFOLD_FILES = (
    ("AGENT-WRITE-RULES.md",    "AGENT-WRITE-RULES.md"),
    (".agent-prefs/README.md",  "agent_prefs_README.md"),
    (".claude/settings.json",   "claude_settings.json"),
    (".gitignore",              "gitignore"),
)

# v26 subdirs that `kb-write init` creates inside kb_root. Two-segment
# paths (topics/standalone-note/, topics/agent-created/) are handled
# by Path.mkdir(parents=True) so the `topics/` parent comes along too.
SUBDIRS = (
    "papers",
    "topics/standalone-note",
    "topics/agent-created",
    "thoughts",
    ".agent-prefs",
)


@dataclass
class InitReport:
    created: list[str]
    refreshed: list[str]
    skipped_existing: list[str]
    overwritten: list[str]


def init_kb(
    kb_root: Path,
    *,
    force: bool = False,
    refresh: bool = False,
) -> InitReport:
    """Create / refresh scaffold files and subdirs.

    force=False, refresh=False (default): additive — missing files
        are created; existing ones untouched.
    refresh=True: re-render prompt files (CLAUDE/AGENTS/README)
        preserving user content appended after the generated block.
        Static scaffolds untouched unless missing.
    force=True: overwrite everything.
    """
    kb_root = Path(kb_root).expanduser().resolve()
    kb_root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    refreshed: list[str] = []
    skipped: list[str] = []
    overwritten: list[str] = []

    for sub in SUBDIRS:
        # v26: some SUBDIRS are two levels deep (topics/standalone-note,
        # topics/agent-created). mkdir(parents=True) lets the parent
        # `topics/` appear automatically.
        (kb_root / sub).mkdir(parents=True, exist_ok=True)

    # 1. Rendered files.
    rendered = prompt_renderer.render_all()
    for filename, new_content in rendered.items():
        dest = kb_root / filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        if not dest.exists():
            atomic_write(dest, new_content)
            created.append(filename)
        elif force:
            atomic_write(dest, new_content)
            overwritten.append(filename)
        elif refresh:
            existing = dest.read_text(encoding="utf-8")
            merged = prompt_renderer.preserve_user_suffix(existing, new_content)
            if merged != existing:
                atomic_write(dest, merged)
                refreshed.append(filename)
            else:
                skipped.append(filename)
        else:
            skipped.append(filename)

    # 2. Static scaffolds.
    for dest_rel, res_name in STATIC_SCAFFOLD_FILES:
        dest = kb_root / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and not force:
            skipped.append(dest_rel)
            continue
        existed_before = dest.exists()
        content = _read_static_scaffold(res_name)
        atomic_write(dest, content)
        # Classify by the PRE-write state, not post-write — post-write
        # `dest.exists()` is always True, so the old `if dest.exists()
        # and force` branch always fired, miscounting new files as
        # overwritten.
        if existed_before:
            overwritten.append(dest_rel)
        else:
            created.append(dest_rel)

    # 3. Workspace config scaffolds. Only do this if kb_root is the
    #    sibling of a `.ee-kb-tools/` directory — otherwise, the user
    #    hasn't opted into the canonical workspace layout, and we
    #    don't want to create files outside kb_root unexpectedly.
    #
    # v25 behaviour: config YAMLs are NEVER overwritten, not even
    # with --force. `--force` is meant to re-scaffold the KB's
    # discovery/template files (CLAUDE.md / AGENTS.md / etc.) which
    # are generated outputs with no user state. The config YAMLs,
    # by contrast, hold the user's own runtime configuration
    # (embedding provider choice, API key env vars, summariser
    # settings) and MUST NOT be stomped. A v24 user reported losing
    # their custom `provider: gemini` setup after an accidental
    # `init --force` — that's exactly the kind of silent data loss
    # this guard prevents. If a user really wants to re-scaffold
    # the config (e.g. to pick up a new template field), they can
    # delete the file manually and re-run init; init will then
    # create the scaffold because the file is missing, not because
    # --force told it to overwrite.
    from ..workspace import TOOLS_DIR_NAME
    tools_dir = kb_root.parent / TOOLS_DIR_NAME
    if tools_dir.exists():
        config_dir = tools_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_scaffolds = (
            ("kb-mcp.yaml", "config_kb_mcp.yaml"),
            ("kb-importer.yaml", "config_kb_importer.yaml"),
            ("kb-citations.yaml", "config_kb_citations.yaml"),
            ("README.md", "config_README.md"),
        )
        for filename, res_name in config_scaffolds:
            dest = config_dir / filename
            # `config/` is outside kb_root, so the report label
            # reflects that.
            rel_label = f"../{TOOLS_DIR_NAME}/config/{filename}"
            if dest.exists():
                # NEVER overwrite existing config — not even with
                # --force. See comment block above.
                skipped.append(rel_label)
                continue
            try:
                content = _read_static_scaffold(res_name)
            except FileNotFoundError:
                # Scaffold resource missing — skip silently rather than
                # crash init.
                continue
            atomic_write(dest, content)
            created.append(rel_label)

    return InitReport(
        created=created,
        refreshed=refreshed,
        skipped_existing=skipped,
        overwritten=overwritten,
    )


def _read_static_scaffold(resource_name: str) -> str:
    if resource_name == "AGENT-WRITE-RULES.md":
        pkg = resources.files("kb_write")
        return (pkg / "AGENT-WRITE-RULES.md").read_text(encoding="utf-8")
    scaffold_pkg = resources.files("kb_write.scaffold")
    return (scaffold_pkg / resource_name).read_text(encoding="utf-8")
