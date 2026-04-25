#!/usr/bin/env python3
"""Fail if any source file tries to autodetect config in system paths.

This enforces the **strict configuration policy**: tools never
read from or write to `~/.config/`, `~/.local/share/`, `/etc/`,
or similar system paths on their own initiative. All configuration
lives in `<workspace>/.ee-kb-tools/config/`.

What's allowed:
  - `Path.expanduser()` applied to user-supplied paths (e.g. when
    the user sets `zotero_storage_dir: ~/Zotero/storage` in YAML)
  - Env vars the user explicitly sets (OPENAI_API_KEY, KB_ROOT, etc.)
  - Mentions in docstrings / comments describing what we DON'T do

What's forbidden in non-comment code:
  - `Path.home() / ".config"` and similar
  - `os.environ.get("XDG_CONFIG_HOME")` / XDG_STATE_HOME / XDG_DATA_HOME
  - Any literal containing `~/.config`, `~/.local/share`, or `/etc/ee-kb`

Run: `python scripts/check_no_system_paths.py`
Exit: 0 clean, 1 violation found.

Intended to be wired into CI eventually.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
# 1.4.7: kb_core added — workspace.py / paths.py live there in v27,
# and the no-system-paths invariant applies to them as much as the
# four downstream packages.
SRC_DIRS = [
    ROOT / "kb_core" / "src",
    ROOT / "kb_importer" / "src",
    ROOT / "kb_mcp" / "src",
    ROOT / "kb_write" / "src",
    ROOT / "kb_citations" / "src",
]

# Forbidden string fragments. These appear in the AST as str constants
# (docstrings and comments are filtered out separately).
FORBIDDEN_STRINGS = [
    "~/.config",
    "~/.local/share",
    "~/.local/state",
    "/etc/ee-kb",
    "/etc/kb-mcp",
    "/etc/kb-importer",
]

# Forbidden env var names. Reading these is how XDG autodetect works —
# we don't do that.
FORBIDDEN_ENV_VARS = {
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
}


class Violation:
    __slots__ = ("file", "line", "kind", "detail")

    def __init__(self, file: Path, line: int, kind: str, detail: str):
        self.file = file
        self.line = line
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        rel = self.file.relative_to(ROOT) if self.file.is_relative_to(ROOT) else self.file
        return f"{rel}:{self.line}: [{self.kind}] {self.detail}"


def iter_py_files() -> list[Path]:
    out = []
    for d in SRC_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def scan_ast(path: Path) -> list[Violation]:
    """AST-level checks: forbidden env var reads, .home()/".config" chains.

    These aren't caught by plain string search because constants live
    inside string-literal nodes that may also be docstrings.
    """
    violations = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return [Violation(path, e.lineno or 0, "parse", str(e))]

    # Collect all docstring node ids so we can skip strings that ARE
    # docstrings.
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstring_ids.add(id(body[0].value))

    for node in ast.walk(tree):
        # os.environ.get("XDG_CONFIG_HOME") or os.environ["XDG_..."]
        if isinstance(node, ast.Call):
            # os.environ.get(...)
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "get" \
                    and isinstance(f.value, ast.Attribute) \
                    and f.value.attr == "environ":
                if node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str) \
                        and node.args[0].value in FORBIDDEN_ENV_VARS:
                    violations.append(Violation(
                        path, node.lineno, "xdg-env",
                        f"reads {node.args[0].value} — forbidden "
                        f"(tools don't autodetect system config paths)"
                    ))

        # os.environ["XDG_..."]
        if isinstance(node, ast.Subscript):
            v = node.value
            if isinstance(v, ast.Attribute) and v.attr == "environ":
                sl = node.slice
                if isinstance(sl, ast.Constant) \
                        and isinstance(sl.value, str) \
                        and sl.value in FORBIDDEN_ENV_VARS:
                    violations.append(Violation(
                        path, node.lineno, "xdg-env",
                        f"reads {sl.value} — forbidden"
                    ))

        # Path.home() / ".config" / ...  (BinOp with / operator)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # Walk up the left side to detect Path.home() / Path("...")
            def _is_path_home(n) -> bool:
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr == "home" \
                        and isinstance(n.func.value, ast.Name) \
                        and n.func.value.id == "Path":
                    return True
                # Recurse through nested BinOp on the left
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
                    return _is_path_home(n.left)
                return False

            def _rhs_is_config_like(n) -> str | None:
                """If RHS is a string constant that's a suspicious system
                path segment, return it; else None."""
                suspicious = {".config", ".local", "etc", ".cache"}
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    if n.value in suspicious:
                        return n.value
                return None

            if _is_path_home(node.left):
                rhs = _rhs_is_config_like(node.right)
                if rhs:
                    violations.append(Violation(
                        path, node.lineno, "home-system-path",
                        f"Path.home() / {rhs!r} — forbidden (system-path "
                        "autodetect)"
                    ))

        # String constants containing forbidden fragments, that aren't
        # docstrings.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            for frag in FORBIDDEN_STRINGS:
                if frag in node.value:
                    violations.append(Violation(
                        path, node.lineno, "forbidden-path",
                        f"string literal contains {frag!r}"
                    ))
                    break

    return violations


def main() -> int:
    violations: list[Violation] = []
    files = iter_py_files()
    for f in files:
        violations.extend(scan_ast(f))

    if not violations:
        print(f"✓ {len(files)} files clean — no system-path autodetect")
        return 0

    print(f"✗ {len(violations)} violation(s) in {len(files)} files:\n")
    for v in violations:
        print(f"  {v}")
    print(
        "\nPolicy: tools never read or write to ~/.config/, "
        "~/.local/share/, /etc/, or similar system paths. "
        "All config lives in <workspace>/.ee-kb-tools/config/."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
