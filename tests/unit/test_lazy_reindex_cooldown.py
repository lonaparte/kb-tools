"""Regression for the v0.27.4 field-report MCP memory leak
(+24 MB RSS / 90 tool calls). tracemalloc showed the Python-
object growth per lazy_reindex was <1 KB; the ~156 KB/call RSS
growth came from SQLite's C-level page + statement cache plus
pymalloc arena retention. The fix: TTL cooldown on
_lazy_reindex so back-to-back tool calls share one scan.

These tests verify:
  1. First call runs the indexer and stamps _last_lazy_reindex_at.
  2. A second call within the cooldown window skips the indexer.
  3. A call after the cooldown window elapses runs again.
  4. Setting COOLDOWN_S=0 disables the cooldown (always run).
  5. A failed reindex does NOT stamp _last_lazy_reindex_at (so the
     next call retries immediately rather than masking the error).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fresh_server_module(monkeypatch, cooldown: float = 1.0):
    """Re-import kb_mcp.server with a patched env var so the module
    reads our desired cooldown. Returns the imported module with
    fresh module-level state."""
    import importlib, sys
    monkeypatch.setenv("KB_MCP_LAZY_REINDEX_COOLDOWN_S", str(cooldown))
    # Force a fresh re-read of the constant.
    if "kb_mcp.server" in sys.modules:
        del sys.modules["kb_mcp.server"]
    import kb_mcp.server as srv
    return srv


def test_first_call_runs_indexer(monkeypatch):
    srv = _fresh_server_module(monkeypatch, cooldown=1.0)

    # Fake config + store so the function doesn't early-return on
    # "server not initialised".
    srv._cfg = MagicMock(kb_root="/tmp", embedding_batch_size=10)
    srv._store = MagicMock()
    srv._embedder = None

    fake_indexer_cls = MagicMock()
    monkeypatch.setattr(srv, "Indexer", fake_indexer_cls)

    srv._lazy_reindex()
    assert fake_indexer_cls.called, (
        "first lazy_reindex should call Indexer"
    )
    assert srv._last_lazy_reindex_at is not None, (
        "timestamp should be stamped on successful scan"
    )


def test_second_call_within_cooldown_skips(monkeypatch):
    srv = _fresh_server_module(monkeypatch, cooldown=5.0)

    srv._cfg = MagicMock(kb_root="/tmp", embedding_batch_size=10)
    srv._store = MagicMock()
    srv._embedder = None

    fake_indexer_cls = MagicMock()
    monkeypatch.setattr(srv, "Indexer", fake_indexer_cls)

    srv._lazy_reindex()
    assert fake_indexer_cls.call_count == 1

    # Call again immediately — cooldown active, should skip.
    srv._lazy_reindex()
    srv._lazy_reindex()
    srv._lazy_reindex()
    assert fake_indexer_cls.call_count == 1, (
        f"within-cooldown calls should skip Indexer; "
        f"got {fake_indexer_cls.call_count} total calls"
    )


def test_call_after_cooldown_runs_again(monkeypatch):
    srv = _fresh_server_module(monkeypatch, cooldown=0.05)

    srv._cfg = MagicMock(kb_root="/tmp", embedding_batch_size=10)
    srv._store = MagicMock()
    srv._embedder = None

    fake_indexer_cls = MagicMock()
    monkeypatch.setattr(srv, "Indexer", fake_indexer_cls)

    srv._lazy_reindex()
    import time
    time.sleep(0.1)  # > 0.05s cooldown
    srv._lazy_reindex()
    assert fake_indexer_cls.call_count == 2


def test_cooldown_zero_disables(monkeypatch):
    """Cooldown=0 should always run the indexer, matching pre-v0.27.5
    behaviour (no skipping). Useful for tests and for users who run
    external edits mid-agent-session."""
    srv = _fresh_server_module(monkeypatch, cooldown=0.0)

    srv._cfg = MagicMock(kb_root="/tmp", embedding_batch_size=10)
    srv._store = MagicMock()
    srv._embedder = None

    fake_indexer_cls = MagicMock()
    monkeypatch.setattr(srv, "Indexer", fake_indexer_cls)

    srv._lazy_reindex()
    srv._lazy_reindex()
    srv._lazy_reindex()
    assert fake_indexer_cls.call_count == 3, (
        "cooldown=0 should disable the skip path entirely"
    )


def test_failed_reindex_does_not_stamp(monkeypatch):
    """If Indexer raises, we log + continue but do NOT stamp
    _last_lazy_reindex_at — otherwise a one-off failure would
    mask subsequent errors for the whole cooldown window."""
    srv = _fresh_server_module(monkeypatch, cooldown=5.0)

    srv._cfg = MagicMock(kb_root="/tmp", embedding_batch_size=10)
    srv._store = MagicMock()
    srv._embedder = None

    class _BoomIndexer:
        def __init__(self, *a, **kw): pass
        def reindex_if_stale(self):
            raise RuntimeError("simulated reindex failure")

    monkeypatch.setattr(srv, "Indexer", _BoomIndexer)

    srv._last_lazy_reindex_at = None
    srv._lazy_reindex()
    # Must NOT have been stamped — a failed scan should be retried
    # on the next call, not skipped due to cooldown.
    assert srv._last_lazy_reindex_at is None, (
        "failed reindex stamped timestamp — would mask future "
        "failures across the cooldown window"
    )


def test_disabled_server_skips_without_stamping(monkeypatch):
    """If the server isn't initialised (_cfg / _store is None),
    lazy_reindex is a no-op and leaves the timestamp alone so the
    first real call after init runs normally."""
    srv = _fresh_server_module(monkeypatch, cooldown=1.0)

    srv._cfg = None
    srv._store = None
    srv._embedder = None

    fake_indexer_cls = MagicMock()
    monkeypatch.setattr(srv, "Indexer", fake_indexer_cls)

    srv._last_lazy_reindex_at = None
    srv._lazy_reindex()
    assert not fake_indexer_cls.called
    assert srv._last_lazy_reindex_at is None
