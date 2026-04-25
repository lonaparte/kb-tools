#!/usr/bin/env python3
"""Lightweight doc-sync release gate.

Catches docs that fall behind code. Specifically:

  1. CHANGELOG.md must have a section for the current VERSION.
  2. README.md must mention the current major.minor version
     somewhere (in install instructions, version banner, etc).
     We DON'T require it to repeat every patch version — patch
     bumps are typically silent.
  3. UPGRADING.md must mention any version that introduces a
     schema bump in its "schema history at a glance" table.
     Skipped if no UPGRADING.md.

Each violation is fatal: this script returns non-zero, gating
release. Designed to take well under a second so it's cheap to run
on every push.

Why these rules and not stricter ones:

  - We deliberately don't insist on a one-to-one CHANGELOG↔README
    feature mention. README is for users, CHANGELOG for maintainers;
    forcing parity flattens the audience distinction.
  - We don't insist that CHANGELOG entry text mention every code
    file changed. That'd be busywork.
  - We DO insist on the version-string presence because that's the
    single fact most likely to drift and most useful to detect
    (someone bumped VERSION but forgot CHANGELOG entirely).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Version-string match. We're tolerant — a trailing pre-release tag
# or build metadata is allowed but not required.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def fail(msg: str) -> None:
    print(f"✗ docs-sync: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    version_file = REPO / "VERSION"
    if not version_file.exists():
        fail("VERSION file missing at repo root")
    raw = version_file.read_text(encoding="utf-8").strip()
    m = _VERSION_RE.match(raw)
    if not m:
        fail(f"VERSION {raw!r} doesn't look like semver MAJOR.MINOR.PATCH")
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    full = f"{major}.{minor}.{patch}"
    minor_str = f"{major}.{minor}"

    # ----- 1. CHANGELOG must have a section for this version. -----
    changelog = REPO / "CHANGELOG.md"
    if not changelog.exists():
        fail("CHANGELOG.md missing")
    changelog_text = changelog.read_text(encoding="utf-8")
    # Match `## [X.Y.Z]` (the format the existing CHANGELOG uses).
    section_re = re.compile(
        rf"^##\s*\[{re.escape(full)}\]", re.MULTILINE,
    )
    if not section_re.search(changelog_text):
        fail(
            f"CHANGELOG.md has no `## [{full}]` section. Add one "
            f"summarising the release before tagging. Bumping "
            f"VERSION without a CHANGELOG entry is the most common "
            f"way docs drift from code."
        )

    # ----- 2. README must mention this major.minor somewhere. -----
    readme = REPO / "README.md"
    if not readme.exists():
        fail("README.md missing")
    readme_text = readme.read_text(encoding="utf-8")
    if minor_str not in readme_text and full not in readme_text:
        fail(
            f"README.md doesn't mention {minor_str} or {full} anywhere. "
            f"For a non-trivial release, surface the new minor in the "
            f"intro / install section / 'what's new' callout."
        )

    # ----- 3. UPGRADING.md (optional): if it exists and contains a
    # schema-history table, the current version's schema must be
    # mentioned IF this release bumped SCHEMA_VERSION. -----
    upgrading = REPO / "UPGRADING.md"
    schema_const_path = (
        REPO / "kb_core" / "src" / "kb_core" / "schema.py"
    )
    if upgrading.exists() and schema_const_path.exists():
        schema_text = schema_const_path.read_text(encoding="utf-8")
        sm = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", schema_text)
        if sm:
            schema_v = sm.group(1)
            up_text = upgrading.read_text(encoding="utf-8")
            # We only enforce that the current schema number is
            # mentioned somewhere in UPGRADING. Doesn't insist on a
            # specific row format; just "is it discoverable here".
            if f"v{schema_v}" not in up_text and f"V{schema_v}" not in up_text:
                fail(
                    f"UPGRADING.md doesn't mention schema v{schema_v}. "
                    f"If the schema bumped, document the migration "
                    f"path; if it didn't, this check is a false "
                    f"positive — adjust the rule rather than adding "
                    f"a misleading entry."
                )

    # All gates green.
    print(
        f"✓ docs-sync: VERSION={full} present in CHANGELOG; "
        f"{minor_str} mentioned in README"
        + (
            "; UPGRADING schema row OK" if upgrading.exists() else ""
        )
    )


if __name__ == "__main__":
    main()
