"""Regression for the v0.27.4 field-report MCP stdio memory leak
(+24 MB RSS / 90 tool calls). The TTL cooldown in
test_lazy_reindex_cooldown.py reduces the lazy_reindex path's
contribution; this file locks the companion fix: a periodic
`malloc_trim(0)` inside `_maybe_trim_arenas` to release
pymalloc arenas + glibc heap back to the OS between tool
bursts.

Live stress with both fixes enabled: RSS grows to ~+12 MB in
round 1 then stays flat across 11 more rounds (96 total calls)
— from a pre-fix "+24 MB linearly growing" baseline. What we
lock here is the *cadence* — a unit test can't easily measure
OS-level heap reclaim, but it can prove the trim call fires
every N invocations of the hook."""
from __future__ import annotations

from unittest.mock import MagicMock


def _fresh_server_module(monkeypatch, every: int = 16, trim_fn=None):
    """Reload kb_mcp.server with the requested trim cadence +
    (optionally) a stubbed malloc_trim callable."""
    import importlib, sys
    monkeypatch.setenv("KB_MCP_MALLOC_TRIM_EVERY", str(every))
    # Also neutralise lazy_reindex so tests are focused.
    monkeypatch.setenv("KB_MCP_LAZY_REINDEX_COOLDOWN_S", "0")
    if "kb_mcp.server" in sys.modules:
        del sys.modules["kb_mcp.server"]
    import kb_mcp.server as srv
    if trim_fn is not None:
        srv._malloc_trim = trim_fn
    return srv


def test_trim_fires_on_Nth_call(monkeypatch):
    """Every Nth call to _maybe_trim_arenas should invoke
    malloc_trim. On intermediate calls the counter advances but
    trim is a no-op."""
    trim_spy = MagicMock(return_value=1)
    srv = _fresh_server_module(monkeypatch, every=4, trim_fn=trim_spy)

    # Calls 1..3: counter goes 1,2,3 — no trim yet.
    srv._maybe_trim_arenas()
    srv._maybe_trim_arenas()
    srv._maybe_trim_arenas()
    assert trim_spy.call_count == 0

    # Call 4: counter hits 4 → trim fires, counter resets to 0.
    srv._maybe_trim_arenas()
    assert trim_spy.call_count == 1
    assert trim_spy.call_args[0] == (0,), (
        "malloc_trim must be called with size=0 (request full "
        "arena release)"
    )

    # Calls 5..7: no trim.
    srv._maybe_trim_arenas()
    srv._maybe_trim_arenas()
    srv._maybe_trim_arenas()
    assert trim_spy.call_count == 1

    # Call 8: trim again.
    srv._maybe_trim_arenas()
    assert trim_spy.call_count == 2


def test_trim_cadence_scales(monkeypatch):
    """With every=16 (default), 50 calls fire ~3 trims (50 // 16 = 3)."""
    trim_spy = MagicMock(return_value=1)
    srv = _fresh_server_module(monkeypatch, every=16, trim_fn=trim_spy)
    for _ in range(50):
        srv._maybe_trim_arenas()
    assert trim_spy.call_count == 3


def test_disabled_when_every_is_zero(monkeypatch):
    """KB_MCP_MALLOC_TRIM_EVERY=0 should skip the whole path —
    `_init_malloc_trim` returns None and `_maybe_trim_arenas`
    is a no-op. Useful for debugging / benchmarking without the
    trim side-effect."""
    srv = _fresh_server_module(monkeypatch, every=0)
    assert srv._malloc_trim is None
    # No exception, just a no-op.
    for _ in range(30):
        srv._maybe_trim_arenas()
    # Counter shouldn't have advanced either (early-return path).
    assert srv._tool_call_counter == 0


def test_non_glibc_platform_is_noop(monkeypatch):
    """On musl / macOS, `ctypes.CDLL('libc.so.6')` either raises
    or the resulting handle has no malloc_trim. In both cases we
    should fall back to None and never trim."""
    srv = _fresh_server_module(monkeypatch, every=4, trim_fn=None)
    # Force-simulate "not available".
    srv._malloc_trim = None
    for _ in range(10):
        srv._maybe_trim_arenas()
    # Counter wasn't even advanced (first branch returns early).
    assert srv._tool_call_counter == 0


def test_trim_exception_is_swallowed(monkeypatch):
    """If malloc_trim raises for some reason (unexpected OS quirk,
    ctypes interop issue), _maybe_trim_arenas must swallow the
    exception — the whole point is a cleanup optimisation, not
    something that can fail the next tool call."""
    import threading
    def boom(_sz):
        raise OSError("simulated malloc_trim failure")

    srv = _fresh_server_module(monkeypatch, every=2, trim_fn=boom)
    # Two calls → trim fires on the 2nd. Must not raise.
    srv._maybe_trim_arenas()
    srv._maybe_trim_arenas()
    # Counter reset occurred before the trim call (matches the
    # "never retry a crashed reindex" pattern from cooldown).
    assert srv._tool_call_counter == 0


def test_lazy_reindex_calls_trim_before_cooldown_check(monkeypatch):
    """The trim hook lives inside _lazy_reindex and runs BEFORE
    the cooldown skip. Reason: the cooldown gates the heavy SQL
    scan, but the memory accumulated from tool impls themselves
    (SELECT results, FTS5 snippets, JSON response strings) still
    needs periodic reclaim. If we only trimmed when the reindex
    actually ran, an agent issuing 100 quick tool calls in <1s
    would trim exactly 0 times. So every lazy_reindex hook is
    a trim-counter tick — reindex cooldown and trim cadence are
    independent."""
    trim_spy = MagicMock(return_value=1)
    srv = _fresh_server_module(monkeypatch, every=3, trim_fn=trim_spy)
    # Install a LONG cooldown so every lazy_reindex would skip the
    # SQL scan; trims should still fire on cadence.
    srv._LAZY_REINDEX_COOLDOWN_S = 3600.0
    srv._cfg = MagicMock(kb_root="/tmp", embedding_batch_size=10)
    srv._store = MagicMock()
    srv._embedder = None
    # Pretend a reindex happened 1 second ago (so cooldown skip fires).
    import time
    srv._last_lazy_reindex_at = time.monotonic() - 1.0

    fake_indexer_cls = MagicMock()
    monkeypatch.setattr(srv, "Indexer", fake_indexer_cls)

    for _ in range(9):
        srv._lazy_reindex()
    # Indexer must NOT have been called (cooldown active).
    assert fake_indexer_cls.call_count == 0
    # But trim should have fired 3 times (every 3 of 9 calls).
    assert trim_spy.call_count == 3
