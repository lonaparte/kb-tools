#!/usr/bin/env bash
# Full pre-release check battery for a 1.x tag.
#
# `make_release.sh` runs only the lint-level gates so quick dev
# iterations don't pay for the full test suite. This script runs
# everything: lints + unit tests + e2e + post-install smoke +
# the release zip build itself.
#
# For a public 1.x release (Production/Stable classifier, git tag
# pushed), all of these MUST pass. The CHANGELOG entry for the
# version should record that this script ran cleanly.
#
# Usage:   scripts/pre_release_full_check.sh
# Expects: an activated venv with all five ee-kb packages installed
#          editable. Fails fast (set -e) on the first failing check.
#
# Exit code: 0 on full pass; non-zero if any gate failed. Note
# that post_install_test.py explicitly DOES NOT fail on missing
# API keys (OpenAI / Gemini / Semantic Scholar); those are skips.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(cat VERSION)"
echo "=== kb-tools full pre-release check for VERSION=$VERSION ==="
echo

# Step 1: lints (same as make_release.sh pre-flight).
echo "--- [1/6] lint battery ---"
python3 scripts/check_package_consistency.py
python3 scripts/check_no_secrets.py
python3 scripts/check_no_system_paths.py
python3 scripts/check_cross_module_imports.py
python3 scripts/check_docs_sync.py
echo

# Step 2: byte-compile every src tree. Catches SyntaxError + simple
# import-time failures without needing pytest.
echo "--- [2/6] byte-compile every src/ tree ---"
python3 -m compileall -q \
    kb_core/src \
    kb_write/src \
    kb_mcp/src \
    kb_importer/src \
    kb_citations/src
echo "  all src/ trees byte-compile clean"
echo

# Step 3: unit tests (stdlib-only runner, ~4s).
echo "--- [3/6] unit tests ---"
python3 scripts/run_unit_tests.py
echo

# Step 4: E2E tests (cross-package integration, ~5s, no network).
echo "--- [4/6] e2e tests ---"
python3 scripts/test_e2e.py
echo

# Step 5: post-install smoke. Exits 0 even if API-keyed tests
# skipped; any non-skip failure is a real problem.
echo "--- [5/6] post-install smoke ---"
python3 scripts/post_install_test.py
echo

# Step 6: build the release zip. Re-runs the lint gates in
# make_release.sh (redundant with step 1 but intentional — make
# sure the release-build path is exercised in full).
echo "--- [6/6] release zip build ---"
bash scripts/make_release.sh
echo

echo "=== FULL PRE-RELEASE CHECK PASSED for $VERSION ==="
echo
echo "The release zip is in dist/kb-tools-$VERSION.zip."
echo "A public 1.x tag requires all six steps green — which they"
echo "just were. Review the CHANGELOG entry for $VERSION and, if"
echo "it records a full-check run, commit & push."
