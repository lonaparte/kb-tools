"""kb-mcp snapshot: export / import the projection DB and caches.

What this covers:
  - .kb-mcp/index.sqlite              (projection DB)
  - .kb-mcp/citations/by-paper/*.json (Phase 4 citation cache)
  - .kb-mcp/similarity-prior.json     (model-agnostic prior, if present)

What this does NOT cover (intentionally — those live in other stores
the user already has their own sync for):
  - ee-kb/papers/*.md, ee-kb/thoughts/*.md, etc.  → git repo
  - ee-kb/.agent-prefs/*.md, ee-kb/.ai-zone/*    → same git repo
  - zotero/storage/**                             → rsync / Zotero sync
  - The user's config (.ee-kb-tools/config/)      → they configure per-machine

Why SQLite's online backup API (v25+):
  We use `sqlite3.Connection.backup()` — SQLite's official online
  backup API — to copy index.sqlite. This:
    - produces a point-in-time consistent snapshot,
    - coexists with concurrent writers (does NOT block a running
      kb-mcp server or kb-importer session),
    - handles WAL-mode databases without extra flags,
    - writes a plain rollback-journal DB at the destination
      regardless of source journal mode (so restoration is a
      straight file rename).
  Previously this used `VACUUM INTO`, which produces the same
  consistency guarantee but holds a write-blocking lock for the
  duration — unsuitable for systemd-timer / always-on-server
  backup scenarios.

The tar is uncompressed by default (sqlite compresses badly at tar
level; users can gzip externally). Add `.tar.gz` extension if you
want compression — we detect it from the filename.

systemd service integration:
  Exit code 0 on success, non-zero on any failure. All progress
  goes to stdout; errors to stderr. No TTY dependencies. Safe to
  run from a systemd.service with `Type=oneshot` and a
  corresponding systemd.timer for scheduling.
"""
from __future__ import annotations

import logging
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..store import default_db_path


log = logging.getLogger(__name__)


# Paths inside the tar are relative to this anchor; `import` restores
# them into <kb_root>/. Keep the archive layout stable — it's a
# contract between versions.
_SNAPSHOT_ROOT = ".kb-mcp"

# Files that matter for restoration. Everything else under .kb-mcp/
# (audit logs, etc.) is NOT included — not a contract, might change.
_DB_REL = f"{_SNAPSHOT_ROOT}/index.sqlite"
_CITATIONS_DIR = f"{_SNAPSHOT_ROOT}/citations"
_PRIOR_REL = f"{_SNAPSHOT_ROOT}/similarity-prior.json"

# Small manifest file written into the archive for future-proofing:
# lets future versions detect what's inside and warn on mismatch.
#
# Version history:
#   1 (v26) — fields: manifest_version, created_at, kb_root_at_export, includes
#   2 (v27) — fields: manifest_version, created_at, includes
#             kb_root_at_export removed to avoid leaking the exporter's
#             host directory structure in shared snapshots.
_MANIFEST_REL = f"{_SNAPSHOT_ROOT}/snapshot-manifest.json"
_MANIFEST_VERSION = 2


def export_snapshot(kb_root: Path, out_path: Path) -> dict:
    """Write a snapshot tar containing the projection DB + caches.

    Returns a dict with `{path, size_bytes, includes}` for the CLI
    to print. Raises FileNotFoundError if index.sqlite is missing
    — can't snapshot what doesn't exist.
    """
    src_db = default_db_path(kb_root)
    if not src_db.exists():
        raise FileNotFoundError(
            f"no index.sqlite at {src_db} — run `kb-mcp index` first"
        )

    # 1. Produce a consistent DB copy via SQLite's online backup API.
    #    Must write to a path that does not yet exist. Anchor tmpdir
    #    to kb_root to avoid writing a 100MB+ result to /tmp (which
    #    may be tmpfs with limited size). The .backup() pathway
    #    coexists with concurrent writes from a running kb-mcp
    #    server — unlike the pre-v25 VACUUM INTO path which held a
    #    write-blocking lock and would stall servers during backup.
    staging_parent = kb_root / _SNAPSHOT_ROOT
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".snapshot-staging-",
        dir=staging_parent,
    ) as tmpdir:
        tmp_db = Path(tmpdir) / "index.sqlite"
        _online_backup(src_db, tmp_db)

        # 2. Build the tar.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w:gz" if out_path.suffix in (".gz", ".tgz") else "w"
        included: list[str] = []
        with tarfile.open(out_path, mode) as tar:
            # DB.
            tar.add(tmp_db, arcname=_DB_REL)
            included.append(_DB_REL)

            # Citations cache (every *.json under by-paper/, if any).
            cite_dir = kb_root / _CITATIONS_DIR / "by-paper"
            if cite_dir.exists():
                count = 0
                for jp in cite_dir.rglob("*.json"):
                    rel = jp.relative_to(kb_root).as_posix()
                    tar.add(jp, arcname=rel)
                    count += 1
                if count:
                    included.append(
                        f"{_CITATIONS_DIR}/by-paper/*.json ({count})"
                    )

            # Similarity prior, if present.
            prior = kb_root / _PRIOR_REL
            if prior.exists():
                tar.add(prior, arcname=_PRIOR_REL)
                included.append(_PRIOR_REL)

            # Manifest (written last so it's at the end of the tar —
            # unextracted readers can still see it via tar list).
            #
            # v27: no longer records `kb_root_at_export`. Prior
            # versions stored the source machine's absolute kb_root
            # path so restore-side diagnostics could check for an
            # accidental self-overwrite, but the field leaked the
            # exporter's username / home-dir layout / mount points
            # into any shared snapshot. The check it enabled was
            # weak (paths on two machines need not match even for
            # the "same" KB), and import already refuses a non-empty
            # target via the explicit --force flag, so the field
            # earns nothing and risks exporting host metadata.
            manifest = {
                "manifest_version": _MANIFEST_VERSION,
                "created_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "includes": included,
            }
            import json as _json
            man_bytes = _json.dumps(manifest, indent=2).encode("utf-8")
            man_tmp = Path(tmpdir) / "manifest.json"
            man_tmp.write_bytes(man_bytes)
            tar.add(man_tmp, arcname=_MANIFEST_REL)

    return {
        "path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "includes": included,
    }


def import_snapshot(
    kb_root: Path,
    in_path: Path,
    *,
    force: bool = False,
) -> dict:
    """Restore a snapshot into kb_root.

    Refuses to overwrite an existing index.sqlite unless `force=True`.
    The citation cache and prior are merged on top of anything that
    exists (snapshot wins on same-named files); this is safe because
    those are derived data with well-defined contents.

    Returns `{restored, skipped}` for the CLI.
    """
    if not in_path.exists():
        raise FileNotFoundError(f"snapshot not found: {in_path}")

    dst_db = default_db_path(kb_root)
    if dst_db.exists() and not force:
        raise FileExistsError(
            f"{dst_db} already exists. Pass --force to overwrite, "
            f"or `kb-mcp reindex --force` to rebuild from scratch "
            f"instead of restoring a snapshot."
        )

    mode = "r:gz" if in_path.suffix in (".gz", ".tgz") else "r"
    restored: list[str] = []
    with tarfile.open(in_path, mode) as tar:
        # Validate manifest if present (not strict — older snapshots
        # won't have one).
        try:
            mf_member = tar.getmember(_MANIFEST_REL)
            fh = tar.extractfile(mf_member)
            if fh:
                import json as _json
                man = _json.loads(fh.read().decode("utf-8"))
                mv = man.get("manifest_version", 0)
                if mv > _MANIFEST_VERSION:
                    log.warning(
                        "snapshot manifest v%s is newer than this "
                        "kb-mcp supports (v%s); proceeding but some "
                        "files may not be understood",
                        mv, _MANIFEST_VERSION,
                    )
        except KeyError:
            log.info("snapshot has no manifest (pre-v1 format); "
                     "proceeding by filename convention")

        # Extract to a staging dir first, then move in — avoids
        # partially-applied imports if the tar is corrupt.
        #
        # CRITICAL: staging dir MUST be on the same filesystem as
        # the destination (kb_root). Otherwise Path.rename/replace
        # raises OSError: Invalid cross-device link — a real gotcha
        # on servers where /tmp is tmpfs but kb_root is on a data
        # disk. Anchor to <kb_root>/.kb-mcp/.snapshot-staging-*/ so
        # we're always on the same mount as dst_db, dst_cite, etc.
        staging_parent = kb_root / _SNAPSHOT_ROOT
        staging_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".snapshot-staging-",
            dir=staging_parent,
        ) as tmpdir:
            staging = Path(tmpdir)
            # SECURITY: guard against path traversal + symlink escape.
            # Reject by full TarInfo (name + type) so symlinks that
            # *look* safe by name can't slip through.
            safe_members = []
            for m in tar.getmembers():
                if not _is_safe_member(m):
                    log.warning(
                        "skipping unsafe snapshot member: %s "
                        "(type=%s)",
                        m.name,
                        "symlink" if m.issym() else
                        "hardlink" if m.islnk() else
                        "device" if m.isdev() else
                        "other",
                    )
                    continue
                safe_members.append(m)
            # v0.27.8: pass filter="data" to satisfy Python 3.14+
            # which deprecates the no-filter default. Our
            # _is_safe_member pre-filter is the primary defense —
            # it's stricter than tarfile's "data" filter (rejects
            # symlinks/hardlinks/devices outright, while "data"
            # allows relative symlinks within the archive). filter
            # acts as belt-and-braces: even if someone loosens
            # _is_safe_member in the future, tarfile still rejects
            # absolute paths, path-traversal (..), and device
            # nodes at extract time. On Py 3.13 the call without
            # filter still worked but emitted a DeprecationWarning
            # that bled into test runs.
            tar.extractall(staging, members=safe_members, filter="data")

            # Move DB.
            staged_db = staging / _DB_REL
            if staged_db.exists():
                dst_db.parent.mkdir(parents=True, exist_ok=True)
                # Also clean up sidecar WAL/SHM from the destination so
                # we don't resurrect stale journal state.
                for sfx in ("-wal", "-shm", "-journal"):
                    sc = dst_db.with_suffix(dst_db.suffix + sfx)
                    if sc.exists():
                        sc.unlink()
                if dst_db.exists():
                    dst_db.unlink()
                staged_db.rename(dst_db)
                restored.append(_DB_REL)

            # Merge citations cache.
            staged_cite = staging / _CITATIONS_DIR / "by-paper"
            if staged_cite.exists():
                dst_cite = kb_root / _CITATIONS_DIR / "by-paper"
                dst_cite.mkdir(parents=True, exist_ok=True)
                n = 0
                for f in staged_cite.glob("*.json"):
                    f.replace(dst_cite / f.name)
                    n += 1
                if n:
                    restored.append(f"{_CITATIONS_DIR}/by-paper/*.json ({n})")

            # Prior.
            staged_prior = staging / _PRIOR_REL
            if staged_prior.exists():
                dst_prior = kb_root / _PRIOR_REL
                dst_prior.parent.mkdir(parents=True, exist_ok=True)
                staged_prior.replace(dst_prior)
                restored.append(_PRIOR_REL)

    return {"restored": restored}


def _online_backup(src: Path, dst: Path) -> None:
    """Write a consistent copy of `src` at `dst` using SQLite's
    online backup API (sqlite3.Connection.backup).

    Rationale (v25+): previously this function used `VACUUM INTO`,
    which produces a consistent point-in-time copy but holds a
    write-blocking lock for the full duration of the copy. A daily
    backup run via `systemctl` would then stall any concurrent
    write from the kb-mcp server (or a concurrent `kb-importer`
    run). The .backup API does incremental page-level copying and
    coexists with ongoing writes — pages that change during the
    copy are re-copied automatically, so the output is still a
    consistent snapshot at the time .backup() returns.

    This is the SQLite-official "online backup" pathway
    (https://www.sqlite.org/backup.html) and the recommended
    production approach. WAL-mode databases are handled correctly
    without any extra flags.

    `dst` must not already exist (caller uses a tempdir anchor).

    Both DELETE and WAL journal modes produce a single-file output
    at `dst` (the backup process writes the full DB state into a
    fresh rollback-journal DB, not a WAL — restoration is then a
    simple file move).
    """
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")

    # Source: open read-only via URI so we don't risk accidental
    # schema migration / journal creation on the live DB. Still
    # coexists with concurrent writers.
    src_uri = f"file:{src}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            # pages=-1 means "copy all remaining pages in one call".
            # For a few-hundred-MB DB this is fine and simpler than
            # looping with progress reporting; if we later need to
            # stream progress, loop with pages=100 and sleep in the
            # callback.
            with dst_conn:
                src_conn.backup(dst_conn, pages=-1, progress=None)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _is_safe_member(member) -> bool:
    """Reject unsafe tar members.

    Path-based rejections:
    - absolute paths ("/etc/passwd")
    - traversal (".." anywhere in parts)
    - anything outside `_SNAPSHOT_ROOT`

    Type-based rejections:
    - symbolic links: can point anywhere at extract time, bypassing
      the path-based check. A member named ".kb-mcp/index.sqlite"
      can legitimately be a symlink to "/etc/shadow"; after extract,
      any write to .kb-mcp/index.sqlite follows the link.
    - hard links: same risk via a different code path.
    - block/char devices, FIFOs: no legitimate use in this archive
      format; disallow by default.

    Only regular files and directories pass.
    """
    name = member.name if hasattr(member, "name") else str(member)
    # Path-based checks.
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    if not name.startswith(_SNAPSHOT_ROOT + "/") and name != _SNAPSHOT_ROOT:
        return False
    # Type-based checks — only run when we have a real TarInfo.
    if hasattr(member, "issym"):
        if member.issym() or member.islnk():
            return False
        if hasattr(member, "isdev") and member.isdev():
            return False
        if hasattr(member, "isfifo") and member.isfifo():
            return False
        # Explicit allow-list: file or dir only.
        if not (member.isfile() or member.isdir()):
            return False
    return True
