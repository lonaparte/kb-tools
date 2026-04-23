#!/usr/bin/env python3
"""Minimal pytest-compatible test runner (stdlib-only).

Runs the tests under `tests/unit/` without requiring pytest to be
installed. Supports the subset of pytest we actually use:

- pytest.fixture (incl. auto-wired `tmp_path` and `monkeypatch`)
- pytest.raises
- pytest.skip
- pytest.fail
- pytest.mark.parametrize
- Classes grouping tests (no special setup required)

Why stdlib-only: `pip install pytest` is not always available in
locked-down build / CI environments. The full pytest test suite is
small enough that a ~200-line runner suffices. Once pytest IS
available, the same tests also run under it unchanged (the
`pytest` module we vendor below is an import-compatible stub).

Usage:
    python3 scripts/run_unit_tests.py           # run all
    python3 scripts/run_unit_tests.py test_paths # match substring
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
for sub in ("kb_core/src", "kb_write/src", "kb_mcp/src",
            "kb_importer/src", "kb_citations/src"):
    p = REPO / sub
    if p.is_dir():
        sys.path.insert(0, str(p))

# v0.27.9: put tests/ on sys.path so unit tests can
# `from conftest import skip_if_no_X`. Actual conftest.py load
# happens AFTER the vendored pytest stub is installed below
# (conftest imports pytest; a real pytest install would satisfy
# it, but in the stdlib-only CI path we rely on our own stub).
_TESTS_DIR = REPO / "tests"
if _TESTS_DIR.is_dir() and str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------
# Vendored pytest-compatible stubs. Just enough for tests/unit/.
# ---------------------------------------------------------------------

class _SkipException(Exception):
    pass


class _FailException(AssertionError):
    pass


class _Raises:
    """Context manager returned by pytest.raises(). Captures the
    exception if raised, re-raises if not (or if wrong type)."""

    def __init__(self, exc_type, match=None):
        self.exc_type = exc_type
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, etype, evalue, tb):
        if etype is None:
            raise AssertionError(
                f"expected {self.exc_type.__name__} to be raised, "
                f"but no exception"
            )
        if not issubclass(etype, self.exc_type):
            return False  # re-raise
        if self.match is not None:
            import re as _re
            if not _re.search(self.match, str(evalue)):
                raise AssertionError(
                    f"exception message {str(evalue)!r} did not "
                    f"match pattern {self.match!r}"
                )
        self.value = evalue
        return True


def _pytest_fixture(fn=None, **_kwargs):
    """Minimal @pytest.fixture. We ignore scope/params — every
    fixture is function-scoped and non-parameterised here.

    Marks the function so our runner can tell fixtures from tests."""
    def _wrap(f):
        f._pytest_fixture = True
        return f
    if fn is not None and callable(fn):
        return _wrap(fn)
    return _wrap


class _ParamDecorator:
    """@pytest.mark.parametrize(name, values) → expand one test per
    value. Our runner detects the _parametrize attribute."""

    def __init__(self, argname, values):
        self.argname = argname
        self.values = values

    def __call__(self, fn):
        fn._parametrize = getattr(fn, "_parametrize", [])
        fn._parametrize.append((self.argname, list(self.values)))
        return fn


class _MarkNamespace:
    @staticmethod
    def parametrize(argname, values):
        # argname may be "a,b" for multi-arg; we only use single-arg
        # in our suite.
        if "," in argname:
            raise NotImplementedError(
                "multi-arg parametrize not supported by this shim"
            )
        return _ParamDecorator(argname, values)

    @staticmethod
    def skipif(condition, reason=""):
        """pytest.mark.skipif — decorator (for fns/classes) or
        module-level sentinel (when assigned to pytestmark).

        If used as a decorator, wraps the target so it skips when
        the condition is True. When assigned to a module-level
        `pytestmark`, our test discovery treats any non-None,
        non-False value as "skip the whole module if pytestmark's
        condition was True". Implementation: we return a callable
        that both decorates AND carries the condition on itself so
        module-level assignment works too.
        """
        def _decorator(obj):
            # Decorator path — mark the callable.
            obj._skipif_condition = bool(condition)
            obj._skipif_reason = reason
            return obj
        # Also carry the condition so `pytestmark = pytest.mark.skipif(...)`
        # is checkable by the discovery code.
        _decorator._skipif_condition = bool(condition)
        _decorator._skipif_reason = reason
        return _decorator


class _PytestModule(ModuleType):
    def __init__(self):
        super().__init__("pytest")
        self.fixture = _pytest_fixture
        self.raises = _Raises
        self.skip = self._skip
        self.fail = self._fail
        self.mark = _MarkNamespace()

    @staticmethod
    def _skip(msg=""):
        raise _SkipException(msg)

    @staticmethod
    def _fail(msg=""):
        raise _FailException(msg)


_pytest_stub = _PytestModule()
sys.modules["pytest"] = _pytest_stub


# v0.27.9: load tests/conftest.py explicitly now that sys.path
# includes tests/ AND our `pytest` stub has been registered
# (conftest.py does `import pytest` at module top for the
# shared skip-guard helpers). Real pytest auto-loads conftest;
# this stdlib-only runner has to do it by hand.
_CONFTEST = _TESTS_DIR / "conftest.py"
if _CONFTEST.is_file():
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("conftest", _CONFTEST)
    if _spec and _spec.loader:
        _mod = _ilu.module_from_spec(_spec)
        sys.modules["conftest"] = _mod
        _spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------
# Built-in fixtures (tmp_path, monkeypatch).
# ---------------------------------------------------------------------

class _MonkeyPatch:
    """Subset of pytest's MonkeyPatch — env vars, chdir, setattr."""

    def __init__(self):
        self._saved_env: dict[str, str | None] = {}
        self._saved_cwd: str | None = None
        self._saved_attrs: list[tuple[object, str, object]] = []

    def setenv(self, name: str, value: str) -> None:
        if name not in self._saved_env:
            self._saved_env[name] = os.environ.get(name)
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in self._saved_env:
            self._saved_env[name] = os.environ.get(name)
        if name in os.environ:
            del os.environ[name]
        elif raising:
            raise KeyError(name)

    def chdir(self, path) -> None:
        if self._saved_cwd is None:
            self._saved_cwd = os.getcwd()
        os.chdir(str(path))

    def setattr(self, target, name, value, raising: bool = True) -> None:
        """Limited setattr(target, name, value) — matches pytest's
        positional form only."""
        if raising and not hasattr(target, name):
            raise AttributeError(name)
        self._saved_attrs.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    def undo(self) -> None:
        for name, old in self._saved_env.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._saved_env.clear()
        if self._saved_cwd is not None:
            try:
                os.chdir(self._saved_cwd)
            except OSError:
                pass
            self._saved_cwd = None
        for target, name, old in reversed(self._saved_attrs):
            try:
                setattr(target, name, old)
            except Exception:
                pass
        self._saved_attrs.clear()


def _make_builtin_fixtures(tmpdir_stack: list):
    """Returns a dict of fixture-name → factory-function."""

    def tmp_path():
        d = tempfile.mkdtemp(prefix="kbtest-")
        tmpdir_stack.append(d)
        return Path(d)

    def monkeypatch():
        mp = _MonkeyPatch()
        tmpdir_stack.append(("monkeypatch", mp))
        return mp

    return {"tmp_path": tmp_path, "monkeypatch": monkeypatch}


# ---------------------------------------------------------------------
# Test discovery + execution.
# ---------------------------------------------------------------------

def _load_test_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"test_module_{path.stem}", path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _module_skip_reason(mod: ModuleType) -> str | None:
    """If the module has `pytestmark = pytest.mark.skipif(...)` with
    a truthy condition, return the reason. Else None.

    Only supports the one-mark, decorator-shaped assignment we
    actually use in tests/unit/; a richer runner would iterate a
    list of marks.
    """
    pm = getattr(mod, "pytestmark", None)
    if pm is None:
        return None
    cond = getattr(pm, "_skipif_condition", None)
    if cond:
        return getattr(pm, "_skipif_reason", "") or "skipif"
    return None


def _collect_tests(mod: ModuleType):
    """Yield (name, callable, fixtures_dict) tuples for each test."""
    module_fixtures = {}
    # Module-level fixtures
    for name, obj in inspect.getmembers(mod):
        if getattr(obj, "_pytest_fixture", False):
            module_fixtures[name] = obj

    # Module-level test functions
    for name, obj in inspect.getmembers(mod, inspect.isfunction):
        if name.startswith("test_") and not getattr(obj, "_pytest_fixture", False):
            yield name, obj, module_fixtures, None

    # Tests inside classes
    for cls_name, cls in inspect.getmembers(mod, inspect.isclass):
        if not cls_name.startswith("Test"):
            continue
        cls_fixtures = dict(module_fixtures)
        for m_name, m_obj in inspect.getmembers(cls, inspect.isfunction):
            if getattr(m_obj, "_pytest_fixture", False):
                cls_fixtures[m_name] = m_obj
        for m_name, m_obj in inspect.getmembers(cls, inspect.isfunction):
            if m_name.startswith("test_") and not getattr(m_obj, "_pytest_fixture", False):
                yield f"{cls_name}::{m_name}", m_obj, cls_fixtures, cls


def _resolve_fixture(
    name: str,
    fixtures: dict,
    builtin_fixtures: dict,
    cache: dict,
):
    """Resolve a fixture to an instance, caching within a test run."""
    if name in cache:
        return cache[name]
    if name in builtin_fixtures:
        val = builtin_fixtures[name]()
        cache[name] = val
        return val
    if name in fixtures:
        fx = fixtures[name]
        sig = inspect.signature(fx)
        kwargs = {}
        for param_name in sig.parameters:
            kwargs[param_name] = _resolve_fixture(
                param_name, fixtures, builtin_fixtures, cache,
            )
        val = fx(**kwargs)
        cache[name] = val
        return val
    raise KeyError(f"fixture not found: {name}")


def _run_one(
    testname: str, fn, fixtures: dict, cls, filter_substr: str,
) -> tuple[str, str]:
    """Run a single test, returning (status, detail). status is one
    of 'PASS', 'FAIL', 'SKIP'."""
    tmpdir_stack: list = []
    builtin_fixtures = _make_builtin_fixtures(tmpdir_stack)
    fixture_cache: dict = {}

    # Parametrize expansion
    params = getattr(fn, "_parametrize", None)
    if params:
        # We only support single-arg single-decorator parametrize.
        argname, values = params[0]
        results = []
        for v in values:
            sub_name = f"{testname}[{v}]"
            if filter_substr and filter_substr not in sub_name:
                continue
            status, detail = _run_single_case(
                sub_name, fn, fixtures, cls, extra_args={argname: v},
                builtin_fixtures=builtin_fixtures,
                fixture_cache={},  # fresh per parametrised case
                tmpdir_stack=[],
            )
            results.append((sub_name, status, detail))
        return results
    else:
        if filter_substr and filter_substr not in testname:
            return [(testname, "FILTERED", "")]
        status, detail = _run_single_case(
            testname, fn, fixtures, cls, extra_args={},
            builtin_fixtures=builtin_fixtures,
            fixture_cache=fixture_cache,
            tmpdir_stack=tmpdir_stack,
        )
        return [(testname, status, detail)]


def _run_single_case(
    testname, fn, fixtures, cls, extra_args,
    builtin_fixtures, fixture_cache, tmpdir_stack,
):
    sig = inspect.signature(fn)
    kwargs = {}
    try:
        params = list(sig.parameters)
        # Skip `self` when fn is a class method.
        if cls is not None and params and params[0] == "self":
            params = params[1:]
        for pname in params:
            if pname in extra_args:
                kwargs[pname] = extra_args[pname]
            else:
                kwargs[pname] = _resolve_fixture(
                    pname, fixtures, builtin_fixtures, fixture_cache,
                )
        if cls is not None:
            instance = cls()
            fn(instance, **kwargs)
        else:
            fn(**kwargs)
        return "PASS", ""
    except _SkipException as e:
        return "SKIP", str(e)
    except BaseException as e:
        # v0.27.4: broadened from Exception to BaseException. A test
        # that triggers argparse's `parser.exit()` or calls
        # `sys.exit()` raises SystemExit — which inherits from
        # BaseException, not Exception. Prior runner let SystemExit
        # propagate, killing the entire test run mid-way and
        # suppressing the summary line. Observed in v0.27.1 field
        # testing when `test_refresh_counts_no_db` passed an
        # argparse-invalid argv; argparse responded with sys.exit(2)
        # and the runner exited rc=2 without printing results.
        #
        # KeyboardInterrupt (Ctrl-C) is a legitimate "stop everything"
        # signal, so let THAT propagate.
        if isinstance(e, KeyboardInterrupt):
            raise
        return "FAIL", traceback.format_exc()
    finally:
        # Cleanup monkeypatch + tmp dirs.
        for item in reversed(tmpdir_stack):
            if isinstance(item, tuple) and item[0] == "monkeypatch":
                item[1].undo()
            else:
                import shutil as _sh
                _sh.rmtree(item, ignore_errors=True)


def main() -> int:
    filter_substr = sys.argv[1] if len(sys.argv) > 1 else ""
    test_files = sorted((REPO / "tests" / "unit").rglob("test_*.py"))

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []

    for tf in test_files:
        try:
            mod = _load_test_module(tf)
        except Exception as e:
            failed += 1
            failures.append((f"{tf.name} IMPORT", traceback.format_exc()))
            continue
        skip_reason = _module_skip_reason(mod)
        if skip_reason:
            # Count each test in the module as skipped rather than
            # silently dropping — keeps the tally honest.
            test_count = 0
            for _ in _collect_tests(mod):
                test_count += 1
            skipped += test_count
            print(f"SKIP {tf.stem}: {skip_reason} ({test_count} tests)")
            continue
        for testname, fn, fixtures, cls in _collect_tests(mod):
            qualified = f"{tf.stem}::{testname}"
            for sub_name, status, detail in _run_one(
                qualified, fn, fixtures, cls, filter_substr,
            ):
                if status == "PASS":
                    passed += 1
                elif status == "SKIP":
                    skipped += 1
                    print(f"SKIP {sub_name}: {detail}")
                elif status == "FILTERED":
                    pass
                else:
                    failed += 1
                    failures.append((sub_name, detail))
                    print(f"FAIL {sub_name}")

    print()
    print(f"=== {passed} passed, {failed} failed, {skipped} skipped ===")
    if failures:
        print("\nFailure details:")
        for name, detail in failures:
            print(f"\n--- {name} ---")
            print(detail)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
