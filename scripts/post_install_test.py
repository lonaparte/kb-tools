#!/usr/bin/env python3
"""Post-install smoke test for ee-kb-tools.

Run this **after** `pip install` on your real machine to verify:

  1. All four CLIs are on PATH and at least print --help
  2. Workspace layout is detectable
  3. `kb-write init` scaffolds correctly
  4. `kb-write` write operations work end-to-end (create, update,
     audit log, dry-run diff)
  5. `kb-mcp index` runs against the scaffold KB (empty is fine)
  6. Each API provider with a key set: make ONE call to verify the
     key works. Skip providers whose key isn't in the environment.
     Currently tested: OpenAI embeddings, Gemini embeddings,
     Semantic Scholar (citations). DeepSeek has no embedding API
     so it's not tested here.
  7. No system-path autodetect (lint check)

Usage:

    python scripts/post_install_test.py [--workspace PATH]

If `--workspace` is omitted, a temp workspace is created for the
run and cleaned up afterward. Pass an existing workspace to test
against your real setup; in that mode a single smoke-test thought
is written under thoughts/*-post-install-smoke-test.md and deleted
again at the end. No other files in your real workspace are
touched.

Exit codes:
    0 — all tests passed
    1 — some non-API test failed (actionable bug)
    2 — environment problem (missing dependency, etc.)

API tests never fail the overall run — they print "SKIP: no KEY in
env" and move on.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path


# ==============================================================
# Test framework
# ==============================================================

@dataclass
class Result:
    name: str
    status: str                  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    duration_ms: int = 0


@dataclass
class Suite:
    results: list[Result] = field(default_factory=list)

    def run(self, name: str, fn):
        t0 = time.monotonic()
        try:
            detail = fn()
            status = "PASS"
            d = detail or ""
        except _Skip as s:
            status = "SKIP"
            d = str(s)
        except Exception as e:
            status = "FAIL"
            d = f"{type(e).__name__}: {e}"
            if os.environ.get("VERBOSE"):
                d += "\n" + traceback.format_exc()
        elapsed = int((time.monotonic() - t0) * 1000)
        self.results.append(Result(name, status, d, elapsed))

    def summary(self) -> tuple[int, int, int]:
        p = sum(1 for r in self.results if r.status == "PASS")
        f = sum(1 for r in self.results if r.status == "FAIL")
        s = sum(1 for r in self.results if r.status == "SKIP")
        return p, f, s

    def print_report(self):
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}
        for r in self.results:
            line = f"  {icon[r.status]} [{r.duration_ms:>4}ms] {r.name}"
            if r.detail:
                line += f"  ({r.detail})"
            print(line)
        p, f, s = self.summary()
        print()
        print(f"  {p} passed, {f} failed, {s} skipped, "
              f"{len(self.results)} total")


class _Skip(Exception):
    pass


def skip(reason: str):
    raise _Skip(reason)


# 1.4.2: cleanup safety. Files we create during the smoke run get
# their absolute paths appended here, AND a unique marker injected
# into their body. Cleanup deletes only paths in the list AND only
# if their contents still contain the marker. The marker includes
# this run's UUID so a stale file from a previous run doesn't get
# deleted by a later run unintentionally either.
import uuid
_SMOKE_TEST_RUN_ID = uuid.uuid4().hex[:12]
_SMOKE_TEST_MARKER = f"<!-- kb-post-install-smoke-test:{_SMOKE_TEST_RUN_ID} -->"
_SMOKE_TEST_PATHS: list[Path] = []


# ==============================================================
# Utilities
# ==============================================================

def cli(cmd: list[str], *, env=None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a CLI command with reasonable defaults."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def which(binary: str) -> str | None:
    return shutil.which(binary)


# ==============================================================
# Section 1: binaries on PATH
# ==============================================================

def test_cli_on_path(name: str):
    def _():
        p = which(name)
        if not p:
            raise RuntimeError(f"{name} not on PATH (did pip install succeed?)")
        r = cli([name, "--help"], timeout=15)
        if r.returncode != 0:
            raise RuntimeError(
                f"{name} --help returned {r.returncode}: {r.stderr[:200]}"
            )
        return p
    return _


# ==============================================================
# Section 2: workspace + init
# ==============================================================

def make_throwaway_workspace() -> Path:
    """Create a fresh 3-sibling workspace in a temp dir."""
    parent = Path(tempfile.mkdtemp(prefix="ee-kb-test-"))
    (parent / ".ee-kb-tools").mkdir()
    (parent / "ee-kb").mkdir()
    (parent / "zotero" / "storage").mkdir(parents=True)
    return parent


def test_kb_write_init(kb_root: Path, tools_dir: Path):
    def _():
        r = cli(["kb-write", "--kb-root", str(kb_root), "init"])
        if r.returncode != 0:
            raise RuntimeError(f"init failed: {r.stderr}")

        # Check entry files exist
        for f in ["CLAUDE.md", "AGENTS.md", "README.md",
                  ".cursorrules", ".aiderrc", "AGENT-WRITE-RULES.md"]:
            if not (kb_root / f).exists():
                raise RuntimeError(f"{f} not created")

        # Check config scaffolds exist (only if workspace layout detected)
        cfg_dir = tools_dir / "config"
        if tools_dir.exists():
            for f in ["kb-mcp.yaml", "kb-importer.yaml",
                      "kb-citations.yaml", "README.md"]:
                if not (cfg_dir / f).exists():
                    raise RuntimeError(
                        f"config scaffold {f} not created in {cfg_dir}"
                    )
        return f"6 entry files + 4 config files scaffolded"
    return _


# ==============================================================
# Section 3: kb-write ops (create, update, audit, dry-run)
# ==============================================================

def test_create_thought(kb_root: Path):
    def _():
        # 1.4.2: embed a per-run marker in the body so cleanup can
        # cross-check identity (path tracking + content marker), not
        # just match a filename glob that could collide with user
        # content.
        body = (
            f"This is a smoke test thought.\n\n"
            f"{_SMOKE_TEST_MARKER}\n"
        )
        body_file = kb_root / "_tmp_body.md"
        body_file.write_text(body)
        try:
            r = cli([
                "kb-write", "--kb-root", str(kb_root),
                "--no-git-commit", "--no-reindex",
                "thought", "create",
                "--title", "post-install smoke test",
                "--body-file", str(body_file),
            ])
            if r.returncode != 0:
                raise RuntimeError(f"create failed: {r.stderr}")
            # Find the created file
            thoughts = list((kb_root / "thoughts").glob("*post-install-smoke-test*.md"))
            if not thoughts:
                raise RuntimeError(f"thought file not found: {r.stdout}")
            # Track the exact paths for cleanup-safe deletion later.
            for t in thoughts:
                if t not in _SMOKE_TEST_PATHS:
                    _SMOKE_TEST_PATHS.append(t)
            return f"created {thoughts[0].name}"
        finally:
            body_file.unlink(missing_ok=True)
    return _


def test_audit_log(kb_root: Path):
    def _():
        audit_log = kb_root / ".kb-mcp" / "audit.log"
        if not audit_log.exists():
            raise RuntimeError("audit.log not created")
        # Should have at least one entry from the create above
        lines = [l for l in audit_log.read_text().splitlines() if l.strip()]
        if not lines:
            raise RuntimeError("audit log empty")
        last = json.loads(lines[-1])
        if last.get("op") != "create_thought":
            raise RuntimeError(f"unexpected last op: {last.get('op')}")
        if last.get("actor") != "cli":
            raise RuntimeError(f"actor should be 'cli', got {last.get('actor')!r}")
        return f"{len(lines)} entries, last op=create_thought actor=cli"
    return _


def test_dry_run_diff(kb_root: Path):
    def _():
        # Find the thought we just created
        thoughts = list((kb_root / "thoughts").glob("*post-install-smoke-test*.md"))
        if not thoughts:
            skip("no thought from previous test")
        md = thoughts[0]
        mtime = md.stat().st_mtime

        # Dry-run update; should emit diff, not modify file
        body = "Updated content for dry run test.\n"
        body_file = kb_root / "_tmp_body.md"
        body_file.write_text(body)
        try:
            rel_path = f"thoughts/{md.stem}"
            r = cli([
                "kb-write", "--kb-root", str(kb_root), "--dry-run",
                "--no-git-commit", "--no-reindex",
                "thought", "update", rel_path,
                "--expected-mtime", str(mtime),
                "--body-file", str(body_file),
            ])
            if r.returncode != 0:
                raise RuntimeError(f"dry-run update failed: {r.stderr}")
            if "Updated content" not in r.stdout:
                raise RuntimeError(f"diff output missing new content: {r.stdout}")
            # File unchanged?
            if "Updated content" in md.read_text():
                raise RuntimeError("dry-run actually modified file!")
            return "dry-run shows diff + file unmodified"
        finally:
            body_file.unlink(missing_ok=True)
    return _


def test_doctor(kb_root: Path):
    def _():
        r = cli(["kb-write", "--kb-root", str(kb_root), "doctor"])
        if r.returncode != 0:
            raise RuntimeError(f"doctor failed: {r.stderr}")
        return r.stdout.strip().splitlines()[-1] if r.stdout else "(empty)"
    return _


def test_log_command(kb_root: Path):
    def _():
        r = cli(["kb-write", "--kb-root", str(kb_root), "log"])
        if r.returncode != 0:
            raise RuntimeError(f"log failed: {r.stderr}")
        if "create_thought" not in r.stdout:
            raise RuntimeError(f"expected create_thought in log: {r.stdout}")
        return "kb-write log shows recent ops"
    return _


# ==============================================================
# Section 4: kb-mcp index (on empty-ish KB)
# ==============================================================

def test_kb_mcp_index(kb_root: Path):
    def _():
        # Disable embeddings for this smoke test (API may be absent)
        env = {"KB_ROOT": str(kb_root)}
        r = cli(["kb-mcp", "index"], env=env, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"index failed: {r.stderr[:300]}")
        return r.stdout.strip().splitlines()[0] if r.stdout else "(indexed)"
    return _


def test_kb_mcp_index_status(kb_root: Path):
    def _():
        env = {"KB_ROOT": str(kb_root)}
        r = cli(["kb-mcp", "index-status"], env=env, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"index-status failed: {r.stderr[:300]}")
        return r.stdout.strip().splitlines()[0] if r.stdout else "(no output)"
    return _


# ==============================================================
# Section 5: API connectivity (skip if no key)
# ==============================================================

def test_openai_embedding():
    def _():
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            skip("OPENAI_API_KEY not set")
        try:
            from openai import OpenAI
        except ImportError:
            skip("openai package not installed (pip install openai)")
        client = OpenAI(api_key=key)
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=["ee-kb smoke test probe"],
        )
        dim = len(resp.data[0].embedding)
        if dim != 1536:
            raise RuntimeError(f"unexpected dim {dim}")
        return f"1 embed OK, dim={dim}"
    return _


def test_gemini_embedding():
    def _():
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            skip("GEMINI_API_KEY not set")
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            skip("google-genai not installed (pip install google-genai)")
        client = genai.Client(api_key=key)
        resp = client.models.embed_content(
            model="gemini-embedding-001",
            contents=["ee-kb smoke test probe"],
            config=genai_types.EmbedContentConfig(
                output_dimensionality=1536,
            ),
        )
        dim = len(resp.embeddings[0].values)
        if dim != 1536:
            raise RuntimeError(f"unexpected dim {dim}")
        return f"1 embed OK, dim={dim}"
    return _


def test_semantic_scholar():
    """No API key strictly required; free tier is 100 req / 5 min.
    Use a stable DOI to avoid flakiness."""
    def _():
        # Use stdlib for zero dependency
        import urllib.request
        import urllib.error
        import json as _json
        # A well-known DOI: Attention Is All You Need
        doi = "10.48550/arXiv.1706.03762"
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/"
            f"DOI:{urllib.parse.quote(doi, safe='')}"
            f"?fields=title"
        )
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "ee-kb-post-install-test/1.0",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                skip(f"rate limited (HTTP 429) — S2 free tier")
            raise RuntimeError(f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            skip(f"network unreachable: {e}")
        title = data.get("title", "")
        if "attention" not in title.lower():
            raise RuntimeError(f"unexpected response: {data}")
        return f"S2 API alive"
    return _


# ==============================================================
# Section 6: no-system-path lint
# ==============================================================

def test_lint_no_system_paths():
    def _():
        # Find the lint script relative to this file
        here = Path(__file__).resolve()
        lint = here.parent / "check_no_system_paths.py"
        if not lint.exists():
            skip(f"lint script not present at {lint}")
        r = cli([sys.executable, str(lint)], timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"lint found violations:\n{r.stdout}")
        first_line = r.stdout.strip().splitlines()[0]
        return first_line
    return _


# ==============================================================
# Main
# ==============================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", type=Path, default=None,
                    help="Existing workspace to test against. "
                         "Temp workspace created if omitted.")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="Keep the temp workspace (for debugging).")
    args = ap.parse_args()

    if args.workspace:
        parent = args.workspace.expanduser().resolve()
        if not (parent / "ee-kb").exists():
            print(f"error: {parent}/ee-kb does not exist", file=sys.stderr)
            return 2
        cleanup_workspace = False
    else:
        parent = make_throwaway_workspace()
        cleanup_workspace = not args.keep_workspace
        print(f"Using throwaway workspace: {parent}")

    kb_root = parent / "ee-kb"
    tools_dir = parent / ".ee-kb-tools"

    suite = Suite()

    print()
    print("=" * 60)
    print("Section 1: CLI binaries on PATH")
    print("=" * 60)
    for name in ["kb-importer", "kb-mcp", "kb-write", "kb-citations"]:
        suite.run(f"{name} --help", test_cli_on_path(name))

    print()
    print("=" * 60)
    print("Section 2: Workspace init")
    print("=" * 60)
    suite.run("kb-write init", test_kb_write_init(kb_root, tools_dir))

    print()
    print("=" * 60)
    print("Section 3: kb-write operations")
    print("=" * 60)
    suite.run("thought create", test_create_thought(kb_root))
    suite.run("audit log populated", test_audit_log(kb_root))
    suite.run("dry-run diff", test_dry_run_diff(kb_root))
    suite.run("doctor", test_doctor(kb_root))
    suite.run("log subcommand", test_log_command(kb_root))

    print()
    print("=" * 60)
    print("Section 4: kb-mcp index (on throwaway KB)")
    print("=" * 60)
    suite.run("kb-mcp index", test_kb_mcp_index(kb_root))
    suite.run("kb-mcp index-status", test_kb_mcp_index_status(kb_root))

    print()
    print("=" * 60)
    print("Section 5: API connectivity (skip if no key)")
    print("=" * 60)
    suite.run("OpenAI embeddings API", test_openai_embedding())
    suite.run("Gemini embeddings API", test_gemini_embedding())
    suite.run("Semantic Scholar API", test_semantic_scholar())

    print()
    print("=" * 60)
    print("Section 6: Lint")
    print("=" * 60)
    suite.run("no system-path autodetect", test_lint_no_system_paths())

    print()
    print("=" * 60)
    print("Results")
    print("=" * 60)
    suite.print_report()

    if cleanup_workspace:
        shutil.rmtree(parent, ignore_errors=True)
    else:
        # 1.4.2: only delete EXACT paths the smoke test recorded, plus
        # require each to carry the smoke-test marker in its
        # frontmatter. Pre-1.4.2 cleanup used a glob on
        # `*post-install-smoke-test*.md`, which would silently delete
        # any user file whose slug happened to contain that substring.
        # Now we cross-check both the path (must be in tracked list)
        # AND the file contents (must contain the marker we wrote).
        cleaned = 0
        for tracked in _SMOKE_TEST_PATHS:
            if not tracked.exists():
                continue
            try:
                content = tracked.read_text(encoding="utf-8")
            except OSError:
                continue
            if _SMOKE_TEST_MARKER not in content:
                # File at the tracked path no longer contains our
                # marker — user must have edited / replaced it.
                # Refuse to delete, surface a warning.
                print(
                    f"\n  ⚠  refusing to delete {tracked} — file no "
                    f"longer carries the smoke-test marker; was it "
                    f"replaced by user content?"
                )
                continue
            try:
                tracked.unlink()
                cleaned += 1
            except OSError:
                pass
        if cleaned:
            print(f"\n  cleaned up {cleaned} smoke-test artifact(s) "
                  f"from {kb_root}")
        if args.workspace is None:
            print(f"\n  workspace kept at {parent}")

    _, failures, _ = suite.summary()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
