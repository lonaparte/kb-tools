"""Regression for v0.27.3 field-report finding #A:
scripts/run_unit_tests.py caught only `Exception` in
_run_single_case, letting SystemExit (raised by argparse
parser.exit() or explicit sys.exit()) propagate and kill the
whole runner without printing a summary. v0.27.4 catches
BaseException (while still propagating KeyboardInterrupt)."""
from __future__ import annotations

import sys
from pathlib import Path


# The runner is a script, not importable by module name. We run
# it as a subprocess here to verify end-to-end behaviour.


def test_runner_survives_systemexit_in_a_test(tmp_path):
    """Write a tiny test file that raises SystemExit(2) and a
    second test that passes. Runner must complete and report
    1 passed, 1 failed, not die after the first."""
    import subprocess

    # Target directory that mimics the runner's tests/unit layout.
    unit = tmp_path / "tests" / "unit"
    unit.mkdir(parents=True)

    # A passing test and a SystemExit-raising test.
    (unit / "test_alpha_pass.py").write_text(
        "def test_alpha():\n"
        "    assert 1 == 1\n"
    )
    (unit / "test_beta_sysexit.py").write_text(
        "import sys\n"
        "def test_beta():\n"
        "    sys.exit(2)\n"
    )

    # Copy the runner into tmp_path (it hardcodes `tests/unit/` as
    # a child of its REPO root).
    repo_root = Path(__file__).resolve().parents[2]
    import shutil
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(
        repo_root / "scripts" / "run_unit_tests.py",
        scripts_dir / "run_unit_tests.py",
    )

    # Run.
    r = subprocess.run(
        [sys.executable, str(scripts_dir / "run_unit_tests.py")],
        capture_output=True, text=True, cwd=tmp_path,
    )
    combined = r.stdout + r.stderr

    # The summary line must have been printed, proving the runner
    # didn't exit early. Before the fix, SystemExit(2) from
    # test_beta would bypass the except clause and kill the runner
    # before it finished collecting the other result.
    assert "passed" in combined and "failed" in combined, (
        f"runner didn't print summary — SystemExit from a test "
        f"killed it. Output:\n{combined!r}"
    )

    # Concrete counts: 1 passed, 1 failed.
    assert "1 passed" in combined, combined
    assert "1 failed" in combined, combined


def test_runner_reports_argparse_sysexit_as_failure(tmp_path):
    """A test that calls into argparse with bad argv — argparse
    calls parser.exit() → SystemExit. Runner should treat this
    as FAIL, not silently-skip."""
    import subprocess

    unit = tmp_path / "tests" / "unit"
    unit.mkdir(parents=True)

    (unit / "test_argparse_fail.py").write_text(
        "import argparse\n"
        "def test_bad_argv():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--required', required=True)\n"
        "    p.parse_args([])  # triggers parser.error → sys.exit(2)\n"
    )

    repo_root = Path(__file__).resolve().parents[2]
    import shutil
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(
        repo_root / "scripts" / "run_unit_tests.py",
        scripts_dir / "run_unit_tests.py",
    )

    r = subprocess.run(
        [sys.executable, str(scripts_dir / "run_unit_tests.py")],
        capture_output=True, text=True, cwd=tmp_path,
    )
    combined = r.stdout + r.stderr
    # Summary line present → runner didn't crash on SystemExit.
    assert "0 passed" in combined and "1 failed" in combined, combined
