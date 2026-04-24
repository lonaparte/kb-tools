#!/usr/bin/env python3
"""Lint: catch missing cross-module imports after the v0.28 package splits.

v0.28 split three large files into sibling modules:
  - kb_write/cli.py              → kb_write/commands/
  - kb_importer/commands/import_cmd.py → import_pipeline.py + import_fulltext.py + import_keys.py
  - kb_mcp/server.py             → server_cli.py (partial)
  - kb_mcp/indexer.py            → embedding_pass.py + stale_cleanup.py + link_resolve.py + _indexer_helpers.py

The split pattern is: a symbol defined in one module is now used
by a sibling. All siblings must explicitly import the symbol.
Missing those explicit imports produces a runtime NameError the
first time that code path runs.

The 0.29.3 bug: `_auto_commit_single_paper` defined in
import_pipeline.py but called from import_fulltext.py without an
import line. Ran fine in most invocations, broke on the specific
code path (fulltext + per-paper git commit) that exercises it.
Unit tests didn't cover that path. This lint is the static safety
net.

## Algorithm

For each split group:
  1. Parse each module; collect DEFINED top-level names
     (functions, classes).
  2. Parse each module's IMPORTED names (from X import Y as Z →
     adds Z).
  3. Parse each module's REFERENCED names (call sites + bare
     Name nodes).
  4. For any module M, flag `name` if:
       - name is referenced in M,
       - name is defined in a sibling S of M,
       - name is NOT imported into M,
       - name is NOT defined locally in M,
       - name doesn't start with `__` (dunders are runtime-magic).

## False positives / skips

  - Names that are both defined locally AND defined in a sibling
    (legitimate shadow / same name coincidence). Local wins.
  - `_` single-char vars, stdlib builtins, etc. — skipped via a
    heuristic stoplist.
"""
from __future__ import annotations

import ast
import pathlib
import sys


REPO = pathlib.Path(__file__).resolve().parent.parent

# Groups of sibling modules that share a "single file before the
# split" history. Each group is a list of (package_dot_path, dir_path).
SPLIT_GROUPS: list[tuple[str, pathlib.Path, list[str]]] = [
    # (label, base_dir, siblings_filenames)
    (
        "kb_importer.commands",
        REPO / "kb_importer" / "src" / "kb_importer" / "commands",
        ["import_cmd.py", "import_keys.py",
         "import_pipeline.py", "import_fulltext.py"],
    ),
    (
        "kb_write.commands",
        REPO / "kb_write" / "src" / "kb_write" / "commands",
        ["_shared.py", "init_cmd.py", "node_cmd.py", "pref_cmd.py",
         "zone_cmd.py", "field_cmd.py", "admin_cmd.py",
         "batch_cmd.py", "migrate_cmd.py"],
    ),
    (
        "kb_mcp (indexer submodules)",
        REPO / "kb_mcp" / "src" / "kb_mcp",
        ["indexer.py", "embedding_pass.py", "stale_cleanup.py",
         "link_resolve.py", "_indexer_helpers.py"],
    ),
    (
        "kb_mcp (server + cli)",
        REPO / "kb_mcp" / "src" / "kb_mcp",
        ["server.py", "server_cli.py"],
    ),
]

# Names that look shared but are actually standalone builtins /
# common variables. Skip to avoid false positives.
SHARED_NAMES_STOPLIST = {
    "self", "cls", "args", "kwargs",
    "Path", "Optional", "Union", "List", "Dict", "Tuple", "Set", "Any",
    "Exception", "RuntimeError", "ValueError", "TypeError", "KeyError",
    "FileNotFoundError", "OSError", "PermissionError",
    "True", "False", "None",
    # locals that happen to collide
    "report", "ctx", "cfg", "result", "item", "key", "value",
}


def _collect_defined(path: pathlib.Path) -> set[str]:
    """Names defined at top level of `path`."""
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
    return out


def _collect_imported(path: pathlib.Path) -> set[str]:
    """Names brought into scope via imports."""
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.asname or alias.name.split(".")[0])
    return out


def _collect_locally_bound(path: pathlib.Path) -> set[str]:
    """Names bound at function/class/module level — both top-level
    and inside function bodies. Conservative superset: we mark
    anything assigned to, imported locally inside a function,
    passed as a parameter, bound in a for/with, or pattern-matched.
    """
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a in node.args.args + node.args.kwonlyargs:
                    out.add(a.arg)
                if node.args.vararg:
                    out.add(node.args.vararg.arg)
                if node.args.kwarg:
                    out.add(node.args.kwarg.arg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for el in t.elts:
                        if isinstance(el, ast.Name):
                            out.add(el.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.target, ast.Name):
                out.add(node.target.id)
            elif isinstance(node.target, (ast.Tuple, ast.List)):
                for el in node.target.elts:
                    if isinstance(el, ast.Name):
                        out.add(el.id)
        elif isinstance(node, ast.With):
            for w in node.items:
                if w.optional_vars and isinstance(w.optional_vars, ast.Name):
                    out.add(w.optional_vars.id)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                out.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.comprehension):
            if isinstance(node.target, ast.Name):
                out.add(node.target.id)
        elif isinstance(node, (ast.ListComp, ast.SetComp,
                                ast.DictComp, ast.GeneratorExp)):
            for g in node.generators:
                if isinstance(g.target, ast.Name):
                    out.add(g.target.id)
        elif isinstance(node, ast.Lambda):
            for a in node.args.args:
                out.add(a.arg)
    return out


def _collect_referenced(path: pathlib.Path) -> set[str]:
    """Every Name used as a Load (read). Skip writes / params."""
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.add(node.id)
    return out


def check_group(label: str, base: pathlib.Path, sibs: list[str]) -> list[str]:
    errs: list[str] = []
    if not base.is_dir():
        return errs
    # defined-per-sibling
    defined_by: dict[str, pathlib.Path] = {}
    for sib in sibs:
        p = base / sib
        if not p.exists():
            continue
        for name in _collect_defined(p):
            # Record first definition; if two siblings both define,
            # leave them alone (shadow case, explicit local wins).
            defined_by.setdefault(name, p)

    for sib in sibs:
        p = base / sib
        if not p.exists():
            continue
        imported = _collect_imported(p)
        bound = _collect_locally_bound(p)
        referenced = _collect_referenced(p)
        for name in sorted(referenced):
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in SHARED_NAMES_STOPLIST:
                continue
            if name not in defined_by:
                continue
            source = defined_by[name]
            if source == p:
                continue                        # same-file; fine
            if name in imported or name in bound:
                continue                        # legitimately brought in
            errs.append(
                f"[{label}] {sib}: references {name!r} from "
                f"{source.name} but never imports it"
            )
    return errs


# ---------------------------------------------------------------------
# Single-file check: stdlib-module referenced but not imported.
#
# Added in 0.29.4 after a review caught import_fulltext.py using
# sys.stderr at 19 sites without `import sys` at the top. The
# cross-module check above didn't help because `sys` isn't defined
# in any sibling — it's a stdlib miss. This second lint closes that
# specific class: for each .py in the repo, collect
# `<name>.<attr>` chains; for every `<name>` in a hardcoded stdlib
# whitelist, assert `<name>` was imported or locally bound.
#
# Whitelist kept small on purpose. The goal is to catch obvious
# paste-forgot-import bugs during module splits; a fully general
# "undefined name" check is what pyflakes/ruff do and we don't want
# to re-implement those. This hits the ~10 stdlib names that actually
# showed up as Name-then-Attribute bugs during the 0.28-0.29 splits.
# ---------------------------------------------------------------------

STDLIB_ROOTS = {
    "sys", "os", "re", "json", "pathlib", "subprocess",
    "threading", "time", "datetime", "logging", "shutil",
    "argparse", "sqlite3", "struct",
}


def check_stdlib_usage(path: pathlib.Path) -> list[str]:
    """For each stdlib module name referenced as `X.attr` in `path`,
    confirm X is imported or locally bound. Otherwise the attribute
    access will raise NameError at runtime."""
    errs: list[str] = []
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return errs
    imported = _collect_imported(path)
    bound = _collect_locally_bound(path)
    # Which stdlib roots does this file actually reference?
    referenced_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if isinstance(node.value.ctx, ast.Load):
                name = node.value.id
                if name in STDLIB_ROOTS:
                    referenced_roots.add(name)
    for name in sorted(referenced_roots):
        if name not in imported and name not in bound:
            errs.append(
                f"{path}: uses {name}.* but never imports {name!r} "
                f"(NameError at runtime on any path that evaluates the attribute)"
            )
    return errs


def check_all_stdlib_usage() -> list[str]:
    errs: list[str] = []
    # Walk all src/ trees.
    for pkg in ("kb_core", "kb_write", "kb_mcp", "kb_importer", "kb_citations"):
        src = REPO / pkg / "src"
        if not src.is_dir():
            continue
        for p in src.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            errs.extend(check_stdlib_usage(p))
    return errs


def main() -> int:
    errors: list[str] = []
    for label, base, sibs in SPLIT_GROUPS:
        errors.extend(check_group(label, base, sibs))
    errors.extend(check_all_stdlib_usage())
    if errors:
        print("✗ cross-module-imports check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("✓ cross-module-imports OK (4 split groups + stdlib-usage scanned, no missing imports)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
