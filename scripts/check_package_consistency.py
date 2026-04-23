#!/usr/bin/env python3
"""Pre-package consistency check.

Run before packaging to catch out-of-sync versions and copy-pasted
modules that have drifted. Exits non-zero on any mismatch, with a
clear error so packaging stops before producing a broken zip.

Usage:
    python3 scripts/check_package_consistency.py

Checks:
  1. VERSION file matches __version__ in all five package __init__s
     (kb_core + kb_write + kb_mcp + kb_importer + kb_citations;
     kb_core's own version is independent, see note below).
  2. kb_write.paths and kb_mcp.paths are pure shims that re-export
     from kb_core — verifies the symbols are IDENTITY-equal, not
     just equivalent. A drift here means someone started adding
     logic back into a shim.
  3. kb_write.workspace and kb_mcp.workspace are pure shims that
     re-export from kb_core.workspace (same identity check).
  4. kb_write/AGENT-WRITE-RULES.md (repo root) and
     kb_write/src/kb_write/AGENT-WRITE-RULES.md (package-data copy)
     are byte-equal.
  5. FULLTEXT_START / FULLTEXT_END marker literals agree between
     kb_importer.md_io and kb_write.ops.re_summarize, and
     re_summarize.SECTION_COUNT matches len(SECTION_TITLES) in
     kb_importer.summarize.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def check_versions() -> list[str]:
    """VERSION file must match __version__ in all five packages
    including kb_core.

    v27: 0.x.y semver alignment — all five packages (kb_core and
    the four bundle packages) ship coordinated. Earlier releases
    treated kb_core's version as independently-ticking; in
    practice it's coupled to the bundle's release cadence, so we
    pin them together.
    """
    errors: list[str] = []
    version_file = REPO / "VERSION"
    if not version_file.exists():
        return ["VERSION file missing at repo root"]
    canonical = version_file.read_text().strip()
    if not canonical:
        return ["VERSION file is empty"]

    packages = [
        "kb_core/src/kb_core/__init__.py",
        "kb_importer/src/kb_importer/__init__.py",
        "kb_mcp/src/kb_mcp/__init__.py",
        "kb_write/src/kb_write/__init__.py",
        "kb_citations/src/kb_citations/__init__.py",
    ]
    pattern = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
    for pkg in packages:
        p = REPO / pkg
        if not p.exists():
            errors.append(f"{pkg}: file missing")
            continue
        m = pattern.search(p.read_text())
        if not m:
            errors.append(f"{pkg}: no __version__ = \"...\" found")
            continue
        if m.group(1) != canonical:
            errors.append(
                f"{pkg}: __version__ = {m.group(1)!r} "
                f"≠ VERSION = {canonical!r}"
            )

    # Also check pyproject.toml versions are synced.
    pyprojects = [
        "kb_core/pyproject.toml",
        "kb_importer/pyproject.toml",
        "kb_mcp/pyproject.toml",
        "kb_write/pyproject.toml",
        "kb_citations/pyproject.toml",
    ]
    pp_pattern = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
    for pp in pyprojects:
        p = REPO / pp
        if not p.exists():
            errors.append(f"{pp}: file missing")
            continue
        m = pp_pattern.search(p.read_text())
        if not m:
            errors.append(f"{pp}: no version = \"...\" found")
            continue
        if m.group(1) != canonical:
            errors.append(
                f"{pp}: version = {m.group(1)!r} "
                f"≠ VERSION = {canonical!r}"
            )

    # v0.27.9: also check INTERNAL cross-dep pins. All five packages
    # ship as one bundle; cross-deps between them must pin the
    # current VERSION exactly (`kb-core==0.27.9`, not `>=0.27.9`).
    # Loose constraints would let pip install a mixed-version
    # bundle (e.g. kb-importer 0.27.9 + kb-write 0.27.8 from a
    # stale wheel cache) where the API contract between packages
    # is formally satisfied but semantically drifted. The project
    # versioning rule is "one unified bundle version; update
    # everything together", so we enforce it at the pyproject
    # level too.
    bundle_names = {
        "kb-core", "kb-write", "kb-mcp", "kb-importer", "kb-citations",
    }
    # Match a dep line inside a dependencies = [ ... ] list. We keep
    # this permissive (don't rely on a TOML parser) to match the
    # style of the rest of this script.
    dep_pattern = re.compile(
        r'"(?P<name>kb-(?:core|write|mcp|importer|citations))'
        r'(?P<op>[=<>!~]+)(?P<ver>[0-9.]+)"'
    )
    for pp in pyprojects:
        p = REPO / pp
        if not p.exists():
            continue
        text = p.read_text()
        for m in dep_pattern.finditer(text):
            name = m.group("name")
            op = m.group("op")
            ver = m.group("ver")
            if name not in bundle_names:
                continue
            if op != "==" or ver != canonical:
                errors.append(
                    f"{pp}: internal cross-dep "
                    f'"{name}{op}{ver}" — must pin '
                    f'"{name}=={canonical}" '
                    f"(one unified bundle version)"
                )
    return errors


def _strip_docstring(src: str) -> str:
    """Drop the leading module docstring so copy-paste comparison
    ignores per-copy header notes. Everything after the first non-
    docstring statement is what we compare.
    """
    # Find end of leading triple-quoted docstring. If none, return as-is.
    m = re.match(r'^\s*"""', src)
    if not m:
        return src
    # Find the next """ after the opening.
    end = src.find('"""', m.end())
    if end < 0:
        return src
    return src[end + 3:].lstrip()


# Symbols that kb_write.paths / kb_mcp.paths MUST re-export from
# kb_core verbatim (identity-equal to the kb_core definition, not a
# local redefinition). If someone adds a local helper with one of
# these names, this check fires.
_CORE_PATHS_SYMBOLS = (
    "PathError", "safe_resolve", "to_relative", "is_book_chapter_filename",
    "PAPERS_DIR", "TOPICS_STANDALONE_DIR", "TOPICS_AGENT_DIR",
    "THOUGHTS_DIR", "ACTIVE_SUBDIRS",
    "NodeAddress", "parse_target", "from_md_path",
)
_CORE_WORKSPACE_SYMBOLS = (
    "Workspace", "WorkspaceError", "resolve_workspace", "find_tools_dir",
    "TOOLS_DIR_NAME", "KB_DIR_NAME", "ZOTERO_DIR_NAME", "ZOTERO_STORAGE_SUBDIR",
)

# Behaviour snapshot for safe_resolve. Kept as a smoke test — even
# though we now identity-check the function is the kb_core one, a
# failure here catches an upstream regression in kb_core itself.
_SAFE_RESOLVE_CASES = [
    # (input, expect_accept)
    ("papers/ABCD1234",          True),
    ("papers/ABCD1234.md",       True),
    ("topics/a/b/c",             True),
    ("",                         False),  # empty
    ("/etc/passwd",              False),  # POSIX absolute
    ("\\Windows\\System32",      False),  # Windows-style absolute
    ("C:/Windows",               False),  # drive letter
    ("D:\\data",                 False),  # drive letter (backslash)
    ("../outside",               False),  # escape via ..
    ("papers/../../etc/passwd",  False),  # escape via .. mid-path
]


def check_core_shim_identity() -> list[str]:
    """Confirm kb_write.paths / kb_mcp.paths / kb_write.workspace /
    kb_mcp.workspace still re-export kb_core symbols by identity.

    A "shim" that silently redeclares a symbol locally (e.g. someone
    adds a hot-patched `safe_resolve` in kb_write.paths while
    debugging and forgets to remove it) would not be caught by
    behaviour tests alone — it would silently diverge in the same
    way the old parity lint was meant to catch. We therefore
    literally `is`-check each symbol against its kb_core source.
    """
    errors: list[str] = []
    import sys as _sys

    paths = [
        REPO / "kb_core/src",
        REPO / "kb_write/src",
        REPO / "kb_mcp/src",
    ]
    prev_path = list(_sys.path)
    for p in paths:
        if str(p) not in _sys.path:
            _sys.path.insert(0, str(p))
    # Evict any stale cached modules so we import fresh.
    for mod in list(_sys.modules):
        if mod in ("kb_core", "kb_write", "kb_mcp") or mod.startswith(
            ("kb_core.", "kb_write.", "kb_mcp.")
        ):
            del _sys.modules[mod]

    try:
        import kb_core  # noqa: F401
        import kb_core.paths as core_paths
        import kb_core.workspace as core_workspace
        import kb_write.paths as write_paths
        import kb_write.workspace as write_workspace
        import kb_mcp.paths as mcp_paths
        import kb_mcp.workspace as mcp_workspace
    except Exception as e:
        _sys.path[:] = prev_path
        return [f"could not import shim modules: {e}"]

    # The kb_core symbol table — actual sources of truth.
    # Some live in kb_core.paths, some in kb_core.addressing, all
    # should be accessible from the top-level kb_core namespace too.
    import kb_core.addressing as core_addr

    for sym in _CORE_PATHS_SYMBOLS:
        # Look up the canonical object in kb_core.
        if hasattr(core_paths, sym):
            canonical = getattr(core_paths, sym)
        elif hasattr(core_addr, sym):
            canonical = getattr(core_addr, sym)
        else:
            errors.append(
                f"kb_core has no symbol {sym!r} — lint list is stale."
            )
            continue
        # kb_write and kb_mcp shims must re-export the same object.
        for shim_name, shim in [
            ("kb_write.paths", write_paths), ("kb_mcp.paths", mcp_paths),
        ]:
            got = getattr(shim, sym, None)
            if got is None:
                errors.append(
                    f"{shim_name} is missing symbol {sym!r}; should "
                    f"re-export from kb_core."
                )
            elif got is not canonical:
                errors.append(
                    f"{shim_name}.{sym} diverged from kb_core: "
                    f"the shim now holds a different object than "
                    f"kb_core.{sym}. This means someone added local "
                    f"logic back into a shim — remove it and re-"
                    f"export from kb_core instead."
                )

    for sym in _CORE_WORKSPACE_SYMBOLS:
        if not hasattr(core_workspace, sym):
            errors.append(
                f"kb_core.workspace has no symbol {sym!r} — lint list is stale."
            )
            continue
        canonical = getattr(core_workspace, sym)
        for shim_name, shim in [
            ("kb_write.workspace", write_workspace),
            ("kb_mcp.workspace", mcp_workspace),
        ]:
            got = getattr(shim, sym, None)
            if got is None:
                errors.append(
                    f"{shim_name} is missing symbol {sym!r}; should "
                    f"re-export from kb_core.workspace."
                )
            elif got is not canonical:
                errors.append(
                    f"{shim_name}.{sym} diverged from kb_core.workspace."
                )

    # Behaviour smoke test — targets kb_core directly since the shims
    # are now guaranteed to be identical references.
    import tempfile
    from pathlib import Path as _Path
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = _Path(tmpdir)
        (kb / "papers").mkdir()
        (kb / "topics" / "a" / "b").mkdir(parents=True)
        for rel, should_accept in _SAFE_RESOLVE_CASES:
            ok = _try(core_paths.safe_resolve, kb, rel, core_paths.PathError)
            if ok != should_accept:
                errors.append(
                    f"kb_core.safe_resolve({rel!r}): expected "
                    f"{'accept' if should_accept else 'reject'}, "
                    f"got {'accept' if ok else 'reject'}"
                )

    _sys.path[:] = prev_path
    return errors


def _try(fn, kb_root, rel, path_err_cls) -> bool:
    """Call safe_resolve; return True on success, False on PathError."""
    try:
        fn(kb_root, rel)
        return True
    except path_err_cls:
        return False
    except Exception:
        # Any other exception is also a rejection from the lint's
        # point of view (we expect only PathError for bad input).
        return False


def check_agent_rules_sync() -> list[str]:
    """AGENT-WRITE-RULES.md exists in two places for historical
    packaging reasons: repo-root copy (user-facing) and inside the
    kb_write package (bundled as package data so kb_write can serve
    it without depending on file layout). Content must be identical;
    drift causes "docs say A, runtime serves B" confusion.
    """
    errors: list[str] = []
    p_root = REPO / "kb_write/AGENT-WRITE-RULES.md"
    p_pkg = REPO / "kb_write/src/kb_write/AGENT-WRITE-RULES.md"
    if not p_root.exists() or not p_pkg.exists():
        if not p_root.exists():
            errors.append("kb_write/AGENT-WRITE-RULES.md missing")
        if not p_pkg.exists():
            errors.append("kb_write/src/kb_write/AGENT-WRITE-RULES.md missing")
        return errors
    if p_root.read_bytes() != p_pkg.read_bytes():
        errors.append(
            "AGENT-WRITE-RULES.md diverged between repo root and "
            "package data. `kb_write/AGENT-WRITE-RULES.md` (repo) "
            "and `kb_write/src/kb_write/AGENT-WRITE-RULES.md` "
            "(package-data) must match. Recommended: edit the "
            "package-data copy as source of truth, then copy to "
            "repo root."
        )
    return errors


def check_fulltext_markers_sync() -> list[str]:
    """Check that FULLTEXT_START / FULLTEXT_END are declared with the
    SAME string literal in kb_importer.md_io (the canonical home) and
    kb_write.ops.re_summarize (a necessary re-declaration to keep
    kb_write independently installable — see that file's docstring).

    Also check that SECTION_COUNT in re_summarize.py matches the
    number of section titles in kb_importer.summarize.SECTION_TITLES.
    These constants are a cross-package string protocol; drift would
    silently break re-summarize without a crash.
    """
    errors: list[str] = []

    # 1. FULLTEXT_START / FULLTEXT_END literal equality.
    importer_md_io = (REPO / "kb_importer" / "src" / "kb_importer" / "md_io.py")
    write_resum    = (REPO / "kb_write"    / "src" / "kb_write"    / "ops" / "re_summarize.py")
    if not importer_md_io.exists():
        return [f"{importer_md_io.relative_to(REPO)} missing"]
    if not write_resum.exists():
        return [f"{write_resum.relative_to(REPO)} missing"]

    def _extract_literal(text: str, name: str) -> str | None:
        m = re.search(
            rf'^{re.escape(name)}\s*=\s*(?P<q>[\'"])(?P<val>[^\'"]*)(?P=q)\s*$',
            text, re.MULTILINE,
        )
        return m.group("val") if m else None

    imp_text = importer_md_io.read_text(encoding="utf-8")
    wri_text = write_resum.read_text(encoding="utf-8")
    for name in ("FULLTEXT_START", "FULLTEXT_END"):
        imp_val = _extract_literal(imp_text, name)
        wri_val = _extract_literal(wri_text, name)
        if imp_val is None:
            errors.append(
                f"{name} not found in kb_importer/src/kb_importer/md_io.py "
                f"(expected `{name} = \"...\"` at module level)"
            )
            continue
        if wri_val is None:
            errors.append(
                f"{name} not found in kb_write/src/kb_write/ops/re_summarize.py "
                f"(expected `{name} = \"...\"` at module level)"
            )
            continue
        if imp_val != wri_val:
            errors.append(
                f"{name} diverged: kb_importer has {imp_val!r}, "
                f"kb_write has {wri_val!r}. These MUST be identical — "
                f"re-summarize splices body between these exact markers."
            )

    # 2. SECTION_COUNT vs len(SECTION_TITLES).
    summarize_py = (REPO / "kb_importer" / "src" / "kb_importer" / "summarize.py")
    sum_text = summarize_py.read_text(encoding="utf-8") if summarize_py.exists() else ""
    # Count entries in SECTION_TITLES = [ ... ] (any string list at module level
    # — we don't parse the AST, we match the simple declared form used there).
    titles_match = re.search(
        r"^SECTION_TITLES\s*=\s*\[(?P<body>[^\]]*)\]",
        sum_text, re.MULTILINE | re.DOTALL,
    )
    if titles_match:
        title_count = sum(
            1 for line in titles_match.group("body").splitlines()
            if line.strip().startswith(('"', "'"))
        )
    else:
        title_count = None

    resum_sc = re.search(
        r"^SECTION_COUNT\s*=\s*(\d+)", wri_text, re.MULTILINE,
    )
    if resum_sc and title_count is not None:
        sc = int(resum_sc.group(1))
        if sc != title_count:
            errors.append(
                f"re_summarize.SECTION_COUNT = {sc} but kb_importer "
                f"has {title_count} entries in SECTION_TITLES — "
                f"re-summarize will mis-splice section bodies."
            )

    return errors


def main() -> int:
    errors: list[str] = []
    errors.extend(check_versions())
    errors.extend(check_core_shim_identity())
    errors.extend(check_agent_rules_sync())
    errors.extend(check_fulltext_markers_sync())
    if errors:
        print("✗ package consistency check failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    # Read VERSION for the success message.
    v = (REPO / "VERSION").read_text().strip()
    print(
        f"✓ package consistency OK (VERSION={v}, kb_core shim identity, "
        f"agent-rules parity, fulltext-markers parity)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
