#!/usr/bin/env bash
# Deploy this kb-tools clone into <workspace-parent>/.ee-kb-tools/
# and install the packages. See DEPLOYMENT.md for the full
# walkthrough; this script automates steps 2-5 (and 7) only —
# steps 1 (confirm parent), 6 (init KB), 8 (config), 9 (first
# import), 10 (delete kb-tools) still need a human or an LLM
# agent acting on human intent.
#
# Usage:
#     cd <repo>
#     ./scripts/deploy.sh <workspace-parent>
#
# Behaviour:
#     - Refuses if <workspace-parent>/.ee-kb-tools already exists.
#     - Refuses if rsync isn't available and `cp -a` path also
#       fails (Windows Git Bash may hit this).
#     - Creates .venv, installs all 5 packages editable.
#     - Runs post_install_test.py at the end.
#
# Exit codes:
#     0  — deployed, tests passed
#     1  — deployed but post-install tests failed
#     2  — refused (existing .ee-kb-tools, missing parent, bad args)
#     3  — install step failed

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <workspace-parent>" >&2
    echo "  <workspace-parent> must contain ee-kb/ and zotero/ as siblings" >&2
    exit 2
fi

WORKSPACE_PARENT="$1"
WORKSPACE_PARENT="${WORKSPACE_PARENT%/}"  # strip trailing slash

# ----- sanity -----
if [ ! -d "$WORKSPACE_PARENT" ]; then
    echo "error: $WORKSPACE_PARENT does not exist" >&2
    exit 2
fi
if [ ! -d "$WORKSPACE_PARENT/ee-kb" ]; then
    echo "warning: $WORKSPACE_PARENT/ee-kb does not exist." >&2
    echo "         You'll need to run 'kb-write init' after deployment." >&2
fi
if [ -e "$WORKSPACE_PARENT/.ee-kb-tools" ]; then
    echo "error: $WORKSPACE_PARENT/.ee-kb-tools already exists." >&2
    echo "       Back it up or remove it before redeploying." >&2
    exit 2
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST="$WORKSPACE_PARENT/.ee-kb-tools"

echo "Deploying from: $SRC"
echo "Deploying to:   $DST"
echo

# ----- copy -----
if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
          --exclude='.venv/' \
          "$SRC/" "$DST/"
else
    echo "rsync not found; falling back to cp -a."
    mkdir -p "$DST"
    # The `.[!.]*` + `*` combo copies hidden files (except '.' and
    # '..') and visible ones. Without this, `cp -a ./ dst/` on some
    # shells skips dotfiles.
    cp -a "$SRC"/. "$DST"/
    find "$DST" -name __pycache__ -type d -prune -exec rm -rf {} +
    find "$DST" -name '*.pyc' -delete
    rm -rf "$DST/.git" "$DST/.venv"
fi

# Sanity: VERSION file present?
if [ ! -f "$DST/VERSION" ]; then
    echo "error: copy failed — VERSION file missing at $DST" >&2
    exit 3
fi

VER="$(cat "$DST/VERSION")"
echo "Copied. VERSION = $VER"

# ----- venv -----
cd "$DST"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null

# ----- install packages in dependency order -----
# Order matters: each package's pyproject.toml declares the others it
# needs as pinned internal deps, so an unmet dep here sends pip off
# to the external index looking for a matching version — which a)
# doesn't exist (we don't publish to PyPI), b) might fetch a stale
# package with the same name. Install topologically:
#   kb_core      — base, no intra-bundle deps
#   kb_write     — needs kb_core
#   kb_importer  — needs kb_core + kb_write
#   kb_mcp       — needs kb_core + kb_write
#   kb_citations — needs kb_core + kb_mcp
echo
echo "Installing packages (editable)..."
for pkg in kb_core kb_write kb_importer kb_mcp kb_citations; do
    echo "  -> $pkg"
    pip install -e "$pkg/" >/dev/null
done

# ----- verify -----
echo
echo "Verifying commands are on PATH:"
for cmd in kb-importer kb-mcp kb-write kb-citations; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  ✓ $cmd → $(which $cmd)"
    else
        echo "  ✗ $cmd not found"
        exit 3
    fi
done

# ----- post-install test -----
echo
echo "Running post-install sanity check..."
if python3 "$DST/scripts/post_install_test.py"; then
    echo
    echo "Deployment complete."
    echo
    echo "Next steps (not automated):"
    echo "  1. Configure $DST/config/kb-importer.yaml and kb-mcp.yaml"
    echo "  2. Set env vars: ZOTERO_API_KEY, OPENAI_API_KEY (or GEMINI_API_KEY)"
    echo "  3. Activate the venv in your shell:"
    echo "       source $DST/.venv/bin/activate"
    echo "     (or add to ~/.bashrc / ~/.zshrc)"
    echo "  4. Try: cd $WORKSPACE_PARENT && kb-importer import papers --limit 5 --dry-run"
    echo "  5. If everything looks right, you can delete the source kb-tools/ clone."
    exit 0
else
    echo
    echo "WARNING: post-install test did not pass cleanly." >&2
    echo "         Deployment files are in place but something is off." >&2
    echo "         Investigate before proceeding." >&2
    exit 1
fi
