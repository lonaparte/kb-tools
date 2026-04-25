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


class InitNonEmptyDirError(Exception):
    """Raised when init would create scaffolds in a non-empty existing
    directory that doesn't already look like a kb_root. See init_kb's
    docstring for the heuristic."""


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

    1.4.2 hardening: refuses to scaffold into a non-empty directory
    that doesn't already look like a kb_root. Specifically, if
    `kb_root` exists, contains files, AND lacks both `.kb-mcp/` and
    any of the canonical scaffold filenames (CLAUDE.md / README.md /
    AGENTS.md / AGENT-WRITE-RULES.md), we raise InitNonEmptyDirError
    so a user who fat-fingered `--kb-root ~` doesn't suddenly find
    a `papers/` and `thoughts/` in their home directory. `--force`
    bypasses the check.
    """
    kb_root = Path(kb_root).expanduser().resolve()

    # 1.4.2: reject "init into pre-existing non-empty non-kb dir"
    # unless --force. The criterion: directory has children AND none
    # of those children look like kb_root markers.
    if kb_root.exists() and kb_root.is_dir() and not force:
        children = list(kb_root.iterdir())
        if children:
            kb_markers = {
                ".kb-mcp", "papers", "thoughts", "topics",
                ".agent-prefs", "AGENT-WRITE-RULES.md",
                "CLAUDE.md", "AGENTS.md", "README.md",
            }
            child_names = {c.name for c in children}
            if not (child_names & kb_markers):
                raise InitNonEmptyDirError(
                    f"refusing to scaffold a KB into {kb_root!s}: "
                    f"directory exists and contains "
                    f"{len(children)} item(s) that don't look like "
                    f"a KB. If this really IS where you want the KB, "
                    f"pass --force; otherwise pick an empty (or new) "
                    f"directory. Common cause: typo'd --kb-root that "
                    f"resolved to your home directory or /tmp."
                )

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
            # 1.4.2: capture mtime before read so the atomic_write
            # below can pass it as expected_mtime, catching any
            # concurrent edit between our read and our write. Pre-1.4.2
            # this read-modify-write was unguarded — the only init
            # path without TOCTOU protection.
            mtime_before = dest.stat().st_mtime
            existing = dest.read_text(encoding="utf-8")
            merged = prompt_renderer.preserve_user_suffix(existing, new_content)
            if merged != existing:
                atomic_write(dest, merged, expected_mtime=mtime_before)
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

    # 3. Workspace config scaffolds.
    #
    #    When kb_root uses the canonical name `ee-kb`, we treat its
    #    parent directory as a workspace parent and will auto-create
    #    `<parent>/.ee-kb-tools/config/` if it doesn't exist. This is
    #    what the user wants in the fresh-workspace scenario (mkdir -p
    #    workspace/{ee-kb,zotero/storage} && cd ee-kb && kb-write init)
    #    — pre-0.29.8 this path silently skipped config scaffolding,
    #    leaving the user with a KB and no config files.
    #
    #    When kb_root has a non-canonical name (e.g. a user pointed
    #    --kb-root at some custom dir like $HOME/research/), we
    #    refuse to auto-create `.ee-kb-tools/` because the parent
    #    directory might be one we shouldn't pollute ($HOME in that
    #    example). Pre-existing `.ee-kb-tools/` still triggers
    #    scaffolding regardless of kb_root name, so the deploy.sh
    #    layout (where `.ee-kb-tools/` is explicitly set up before
    #    init) is unaffected.
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
    from ..workspace import TOOLS_DIR_NAME, KB_DIR_NAME
    tools_dir = kb_root.parent / TOOLS_DIR_NAME
    canonical_kb_name = (kb_root.name == KB_DIR_NAME)
    should_scaffold_config = tools_dir.exists() or canonical_kb_name
    if should_scaffold_config:
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
            except FileNotFoundError as e:
                # 0.29.3: fail loud. Pre-0.29.3 this was a silent
                # `continue`, which meant a packaging regression
                # (scaffold file missing from the wheel) produced a
                # KB with no config and no error. That class of
                # failure is how 0.29.1 / 0.29.2 both shipped
                # without the three config yamls. Refuse to continue
                # if kb-write was installed from a broken package;
                # scripts/check_package_consistency also asserts
                # these files are present as a release-time gate,
                # but this runtime check is the second line of
                # defense for the user who actually tries `kb-write
                # init`.
                raise RuntimeError(
                    f"packaging error: kb-write is missing scaffold "
                    f"resource {res_name!r} (needed for "
                    f".ee-kb-tools/config/{filename}). Re-install "
                    f"kb-write from a correctly built wheel — the "
                    f"file should live at kb_write/scaffold/ inside "
                    f"the installed package."
                ) from e
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
