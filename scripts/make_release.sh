#!/usr/bin/env bash
# Build a clean release zip of kb-tools.
#
# Problem this exists to solve: `zip -r kb-tools-<v>.zip kb-tools/`
# over the working tree picks up 187+ `__pycache__/` and `*.pyc`
# entries from local test runs (python interpreter dumps bytecode
# beside the source it just imported). That:
#   - bloats the zip,
#   - leaks the packager's Python minor version into the artefact
#     (bytecode is interpreter-specific),
#   - can cause weird import behaviour on the receiver if their
#     Python differs from the one that produced the .pyc.
#
# This script stages a sanitised copy, cleans bytecode + other
# transient artefacts, verifies no secrets / system-paths / version
# drift, and zips.
#
# Usage:   scripts/make_release.sh [OUTPUT_DIR]
#          (default OUTPUT_DIR = ./dist/)
# Output:  dist/kb-tools-<VERSION>.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(cat VERSION)"
OUT_DIR="${1:-dist}"
mkdir -p "$OUT_DIR"
# Resolve to absolute path so subshells that `cd` elsewhere still
# write to the right place.
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
OUT_ZIP="$OUT_DIR/kb-tools-$VERSION.zip"

# Sanity gates before we build the artefact — fail fast so we don't
# ship a known-broken zip. Each check is `set -e`-wired; failure of
# any one aborts the release.
echo "=== pre-flight checks ==="
python3 scripts/check_package_consistency.py
python3 scripts/check_no_secrets.py
python3 scripts/check_no_system_paths.py
# 0.29.3: catches the _auto_commit_single_paper class of bug — a
# symbol used in one split-file sibling but never imported. A
# missing cross-module import compiles (NameError only fires at
# runtime on the specific path) so linting is the only way to
# block it before release.
python3 scripts/check_cross_module_imports.py

# Stage into a temp dir, filter on rsync.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo
echo "=== staging into $STAGE ==="
DEST="$STAGE/kb-tools-$VERSION"
mkdir -p "$DEST"

# Portable copy: prefer rsync if available (faster on large trees),
# fall back to cp -a + post-filter.
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude='.git/' \
    --exclude='.venv*/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.pytest_cache/' \
    --exclude='.mypy_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='dist/' \
    --exclude='build/' \
    --exclude='*.egg-info/' \
    --exclude='.DS_Store' \
    "$ROOT"/ "$DEST/"
else
  # cp -a preserves perms + timestamps. We copy everything then
  # scrub; the find sweep below is the source of truth for what's
  # excluded.
  cp -a "$ROOT"/. "$DEST/"
fi

# Sweep: nuke every transient artefact regardless of which copy path
# ran. This is the authoritative filter — rsync excludes are an
# optimisation, not a guarantee.
find "$DEST" -type d \( \
    -name __pycache__ -o \
    -name '.pytest_cache' -o \
    -name '.mypy_cache' -o \
    -name '.ruff_cache' -o \
    -name '.venv' -o \
    -name '.venv314' -o \
    -name 'dist' -o \
    -name 'build' -o \
    -name '*.egg-info' \
  \) -prune -exec rm -rf {} +
find "$DEST" -type f \( \
    -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \
  \) -delete
# Don't ship .git — release receiver has no use for it.
rm -rf "$DEST/.git"

# Verify the sanitised tree is free of transient artefacts.
LEFT_PYCACHE="$(find "$STAGE" -type d -name __pycache__ | wc -l)"
LEFT_PYC="$(find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' \) | wc -l)"
if [ "$LEFT_PYCACHE" -ne 0 ] || [ "$LEFT_PYC" -ne 0 ]; then
  echo "error: cleanup left pycache=$LEFT_PYCACHE pyc=$LEFT_PYC" >&2
  exit 1
fi

# Build the archive. Prefer `zip` (matches what reviewers get); fall
# back to `python3 -m zipfile` which is in stdlib everywhere.
echo
echo "=== building $OUT_ZIP ==="
if command -v zip >/dev/null 2>&1; then
  (cd "$STAGE" && zip -qr "$OUT_ZIP" "kb-tools-$VERSION")
else
  python3 -m zipfile -c "$OUT_ZIP" "$STAGE/kb-tools-$VERSION"
fi

# Post-build report so the packager can eyeball the result.
SIZE="$(du -h "$OUT_ZIP" | awk '{print $1}')"
ENTRIES="$(python3 -c "import zipfile,sys; print(len(zipfile.ZipFile(sys.argv[1]).namelist()))" "$OUT_ZIP")"
echo
echo "=== built ==="
echo "  path:    $OUT_ZIP"
echo "  size:    $SIZE"
echo "  entries: $ENTRIES"

# Final check: make sure the zip itself contains zero pycache.
PYC_IN_ZIP="$(python3 -c "
import zipfile, sys, re
names = zipfile.ZipFile(sys.argv[1]).namelist()
bad = [n for n in names if re.search(r'__pycache__|\.pyc\$|\.pyo\$', n)]
print(len(bad))
" "$OUT_ZIP")"
if [ "$PYC_IN_ZIP" -ne 0 ]; then
  echo "error: zip contains $PYC_IN_ZIP bytecode entries" >&2
  exit 1
fi
echo "  bytecode entries: 0 ✓"
