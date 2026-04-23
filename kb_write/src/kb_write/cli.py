"""kb-write command-line entry point.

Subcommands:
    init                     — scaffold KB with discovery files
    thought create/update    — thought md ops
    topic create/update      — topic md ops
    pref add/update/list/show — agent preferences
    rules                    — print AGENT-WRITE-RULES.md to stdout
    doctor                   — (stub; Phase 3b)

Deliberately uses argparse rather than click — no extra dep for
local agents to install.
"""
from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from .config import WriteContext, kb_root_from_env
from .ops import thought as thought_ops
from .ops import topic as topic_ops
from .ops import preference as pref_ops
from .ops import init as init_ops
from .ops import ai_zone as ai_zone_ops
from .ops import tag as tag_ops
from .ops import ref as ref_ops
from .ops import delete as delete_ops
from .ops import doctor as doctor_ops
from .paths import PathError
from .rules import RuleViolation
from .atomic import WriteConflictError, WriteExistsError
from .zones import ZoneError


def _positive_int(value: str) -> int:
    """argparse `type=` helper: accept positive ints, reject others.

    Returns the int on success. Raises argparse.ArgumentTypeError
    otherwise; argparse then prints a uniform "invalid value" error
    message (much clearer than silent defaults or `ValueError`
    stack traces mid-run).
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"must be an integer, got {value!r}"
        )
    if n <= 0:
        raise argparse.ArgumentTypeError(
            f"must be positive, got {n}"
        )
    return n


def _fmt_path(path: Path, kb_root: Path, *, absolute: bool) -> str:
    """Thin delegate to kb_core.format.render_path so behaviour is
    shared with other packages. Kept as a local wrapper because the
    rest of this module calls it as a two-line helper; swapping in
    a direct `render_path(...)` call everywhere would be noisy.
    """
    from kb_core.format import render_path
    return render_path(path, kb_root, absolute=absolute)


def _parser() -> argparse.ArgumentParser:
    from . import __version__
    p = argparse.ArgumentParser(
        prog="kb-write",
        description="Write layer for the ee-kb knowledge base. "
                    "See AGENT-WRITE-RULES.md for invariants.",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s v{__version__}")
    p.add_argument("--kb-root", type=Path,
                   help="KB repo root (defaults to $KB_ROOT).")
    p.add_argument("--no-git-commit", action="store_true",
                   help="Skip git auto-commit for this operation.")
    p.add_argument("--no-reindex", action="store_true",
                   help="Skip `kb-mcp index` after writing.")
    p.add_argument("--no-lock", action="store_true",
                   help="Skip write lock (unsafe; for debugging).")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate everything but don't write.")
    p.add_argument("--commit-message", "-m",
                   help="Extra body for the git commit message.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON for stdout.")
    p.add_argument("--absolute", action="store_true",
                   help="Print absolute paths instead of kb-relative "
                        "paths in human output. Useful when you need "
                        "to pipe into `vim` / `cd` / `ls` etc. JSON "
                        "output always uses kb-relative paths "
                        "regardless of this flag.")

    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    _add_init_cmd(sub)
    _add_thought_cmds(sub)
    _add_topic_cmds(sub)
    _add_pref_cmds(sub)
    _add_ai_zone_cmds(sub)
    _add_tag_cmds(sub)
    _add_ref_cmds(sub)
    _add_delete_cmd(sub)
    _add_log_cmd(sub)
    _add_rules_cmd(sub)
    _add_doctor_cmd(sub)
    _add_re_summarize_cmd(sub)
    _add_re_read_cmd(sub)
    _add_migrate_legacy_chapters_cmd(sub)

    return p


# ---------- init ----------
def _add_init_cmd(sub):
    p = sub.add_parser("init", help="Scaffold a KB with discovery files.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite ALL scaffold files (destructive).")
    p.add_argument("--refresh", action="store_true",
                   help="Re-render prompt files (CLAUDE.md/AGENTS.md/README.md) "
                        "from the latest fragments, preserving any user content "
                        "appended AFTER the generated block.")
    p.set_defaults(func=_cmd_init)


def _cmd_init(args, ctx: WriteContext | None):
    kb_root = _resolve_kb_root(args, allow_missing=True)
    report = init_ops.init_kb(
        kb_root, force=args.force, refresh=args.refresh,
    )
    if args.json:
        print(json.dumps({
            "kb_root": str(kb_root),
            "created": report.created,
            "refreshed": report.refreshed,
            "skipped_existing": report.skipped_existing,
            "overwritten": report.overwritten,
        }, indent=2))
    else:
        print(f"kb-write init: {kb_root}")
        for f in report.created:
            print(f"  created    {f}")
        for f in report.refreshed:
            print(f"  refreshed  {f}  (generated block updated, user suffix preserved)")
        for f in report.overwritten:
            print(f"  overwrote  {f}")
        for f in report.skipped_existing:
            print(f"  skipped    {f} (already exists; use --refresh or --force)")
        if not (report.created or report.refreshed or report.overwritten):
            print("  nothing to do.")
    return 0


# ---------- thought ----------
def _add_thought_cmds(sub):
    t = sub.add_parser("thought", help="Thought md ops.")
    ts = t.add_subparsers(dest="thought_cmd", required=True)

    c = ts.add_parser("create", help="Create a new thought.")
    c.add_argument("--title", required=True)
    c.add_argument("--slug", help="Optional; auto-generated from title + today if absent.")
    c.add_argument("--body-file", type=Path, required=True,
                   help="Path to a file containing the md body (or '-' for stdin).")
    c.add_argument("--ref", action="append", default=[], dest="refs",
                   help="kb_refs entry (repeatable).")
    c.add_argument("--tag", action="append", default=[], dest="tags",
                   help="kb_tags entry (repeatable).")
    c.set_defaults(func=_cmd_thought_create)

    u = ts.add_parser("update", help="Update an existing thought.")
    u.add_argument("target",
                   help="thoughts/SLUG or SLUG.")
    u.add_argument("--expected-mtime", type=float, required=True,
                   help="mtime from your last read (conflict guard).")
    u.add_argument("--body-file", type=Path,
                   help="New body (optional; omit to leave body unchanged).")
    u.add_argument("--title")
    u.add_argument("--ref", action="append", dest="refs")
    u.add_argument("--refs-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.add_argument("--tag", action="append", dest="tags")
    u.add_argument("--tags-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.set_defaults(func=_cmd_thought_update)


def _cmd_thought_create(args, ctx):
    body = _read_body(args.body_file)
    result = thought_ops.create(
        ctx, title=args.title, body=body,
        slug=args.slug, refs=args.refs, tags=args.tags,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_thought_update(args, ctx):
    body = _read_body(args.body_file) if args.body_file else None
    target = args.target
    if not target.startswith("thoughts/") and "/" not in target:
        target = f"thoughts/{target}"
    result = thought_ops.update(
        ctx, target, expected_mtime=args.expected_mtime,
        body=body, title=args.title,
        refs=args.refs, refs_mode=args.refs_mode,
        tags=args.tags, tags_mode=args.tags_mode,
    )
    _emit_result(args, ctx, result)
    return 0


# ---------- topic ----------
def _add_topic_cmds(sub):
    t = sub.add_parser("topic", help="Topic md ops.")
    ts = t.add_subparsers(dest="topic_cmd", required=True)

    c = ts.add_parser("create", help="Create a new topic.")
    c.add_argument("--slug", required=True)
    c.add_argument("--title", required=True)
    c.add_argument("--body-file", type=Path, required=True)
    c.add_argument("--ref", action="append", default=[], dest="refs")
    c.add_argument("--tag", action="append", default=[], dest="tags")
    c.set_defaults(func=_cmd_topic_create)

    u = ts.add_parser("update", help="Update an existing topic.")
    u.add_argument("target")
    u.add_argument("--expected-mtime", type=float, required=True)
    u.add_argument("--body-file", type=Path)
    u.add_argument("--title")
    u.add_argument("--ref", action="append", dest="refs")
    u.add_argument("--refs-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.add_argument("--tag", action="append", dest="tags")
    u.add_argument("--tags-mode", choices=["replace", "add", "remove"],
                   default="replace")
    u.set_defaults(func=_cmd_topic_update)


def _cmd_topic_create(args, ctx):
    body = _read_body(args.body_file)
    result = topic_ops.create(
        ctx, slug=args.slug, title=args.title, body=body,
        refs=args.refs, tags=args.tags,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_topic_update(args, ctx):
    body = _read_body(args.body_file) if args.body_file else None
    target = args.target
    if not target.startswith("topics/") and "/" not in target:
        target = f"topics/{target}"
    result = topic_ops.update(
        ctx, target, expected_mtime=args.expected_mtime,
        body=body, title=args.title,
        refs=args.refs, refs_mode=args.refs_mode,
        tags=args.tags, tags_mode=args.tags_mode,
    )
    _emit_result(args, ctx, result)
    return 0


# ---------- pref ----------
def _add_pref_cmds(sub):
    p = sub.add_parser("pref", help="Agent preferences (.agent-prefs/).")
    ps = p.add_subparsers(dest="pref_cmd", required=True)

    add = ps.add_parser("add", help="Create a new preference file.")
    add.add_argument("--slug", required=True)
    add.add_argument("--body-file", type=Path, required=True)
    add.add_argument("--scope", default="global",
                     help="Scope tag (global/writing/research/ai-summary/...).")
    add.add_argument("--priority", type=int, default=50)
    add.add_argument("--title")
    add.set_defaults(func=_cmd_pref_add)

    up = ps.add_parser("update", help="Update an existing preference.")
    up.add_argument("slug")
    up.add_argument("--expected-mtime", type=float, required=True)
    up.add_argument("--body-file", type=Path)
    up.add_argument("--scope")
    up.add_argument("--priority", type=int)
    up.add_argument("--title")
    up.set_defaults(func=_cmd_pref_update)

    ls = ps.add_parser("list", help="List all preferences.")
    ls.set_defaults(func=_cmd_pref_list)

    sh = ps.add_parser("show", help="Print a preference file's content.")
    sh.add_argument("slug", nargs="?",
                    help="If omitted, dumps all prefs formatted for agent.")
    sh.add_argument("--scope", help="Filter by scope when dumping all.")
    sh.set_defaults(func=_cmd_pref_show)


def _cmd_pref_add(args, ctx):
    body = _read_body(args.body_file)
    result = pref_ops.create(
        ctx, slug=args.slug, body=body,
        scope=args.scope, priority=args.priority,
        title=args.title,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_pref_update(args, ctx):
    body = _read_body(args.body_file) if args.body_file else None
    result = pref_ops.update(
        ctx, slug=args.slug, expected_mtime=args.expected_mtime,
        body=body, scope=args.scope, priority=args.priority,
        title=args.title,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_pref_list(args, ctx):
    kb_root = _resolve_kb_root(args)
    prefs = pref_ops.list_all(kb_root)
    if args.json:
        print(json.dumps(prefs, indent=2, default=str))
        return 0
    if not prefs:
        print("(no preferences yet)")
        return 0
    for p in prefs:
        print(f"  [{p['scope']}, pri={p['priority']}, updated={p['last_updated']}] "
              f"{p['slug']}: {p['title']}")
        print(f"      path: {p['path']}, mtime={p['mtime']:.9f}")
    return 0


def _cmd_pref_show(args, ctx):
    kb_root = _resolve_kb_root(args)
    if args.slug:
        # Validate slug the same way create/update/delete do, so a
        # caller passing "../something" can't read outside the
        # .agent-prefs/ dir. Previously this CLI skipped validation —
        # letting `kb-write pref show ../../../etc/passwd` actually
        # try to read /etc/passwd.md. _validate_pref_slug rejects
        # the `/` and the `..` shapes both.
        from .ops.preference import _validate_pref_slug
        try:
            _validate_pref_slug(args.slug)
        except RuleViolation as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

        md_path = (kb_root / ".agent-prefs" / f"{args.slug}.md").resolve()
        # Defense-in-depth: even after slug validation, hard-check
        # that the resolved path stays inside .agent-prefs/.
        prefs_root = (kb_root / ".agent-prefs").resolve()
        try:
            md_path.relative_to(prefs_root)
        except ValueError:
            print(
                f"error: resolved path {md_path} escapes "
                f"{prefs_root}; refusing to read.",
                file=sys.stderr,
            )
            return 2

        if not md_path.exists():
            print(f"error: no such preference: {args.slug}", file=sys.stderr)
            return 1
        print(md_path.read_text(encoding="utf-8"))
        print(f"\n<!-- mtime: {md_path.stat().st_mtime:.9f} -->")
    else:
        print(pref_ops.read_all_for_agent(kb_root, scope=args.scope or "all"))
    return 0


# ---------- ai-zone ----------
def _add_ai_zone_cmds(sub):
    p = sub.add_parser(
        "ai-zone",
        help="Append a dated entry to the AI zone of a paper/note md."
    )
    ps = p.add_subparsers(dest="ai_zone_cmd", required=True)

    # v26: append replaces the old `update` (which did full replace).
    a = ps.add_parser(
        "append",
        help=(
            "Insert a new dated entry at the top of the AI zone. "
            "Older entries are preserved verbatim — append-only. "
            "Each entry gets `### YYYY-MM-DD — <title>` as its heading."
        ),
    )
    a.add_argument(
        "target",
        help="Which md to edit. E.g. papers/ABCD1234, "
             "papers/BOOKKEY-ch03 (a book chapter), or "
             "topics/standalone-note/NOTEKEY.",
    )
    a.add_argument("--expected-mtime", type=float, required=True)
    a.add_argument(
        "--title", required=True,
        help="One-line title for this entry (no newlines).",
    )
    a.add_argument(
        "--body-file", type=Path, required=True,
        help="Path to file with entry body; '-' for stdin.",
    )
    a.add_argument(
        "--date", default=None,
        help="Override entry date (ISO YYYY-MM-DD). Defaults to today.",
    )
    a.set_defaults(func=_cmd_ai_zone_append)

    r = ps.add_parser("show", help="Print current AI-zone body + mtime.")
    r.add_argument("target")
    r.set_defaults(func=_cmd_ai_zone_show)


def _cmd_ai_zone_append(args, ctx):
    from datetime import date as _date
    entry_date = _date.fromisoformat(args.date) if args.date else None
    body = _read_body(args.body_file)
    result = ai_zone_ops.append(
        ctx, args.target,
        expected_mtime=args.expected_mtime,
        title=args.title,
        body=body,
        entry_date=entry_date,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_ai_zone_show(args, ctx):
    kb_root = _resolve_kb_root(args)
    body, mtime = ai_zone_ops.read_zone(kb_root, args.target)
    if args.json:
        print(json.dumps({"body": body, "mtime": mtime}, indent=2))
    else:
        print(body)
        print(f"\n<!-- mtime: {mtime:.9f} -->")
    return 0


# ---------- tag ----------
def _add_tag_cmds(sub):
    p = sub.add_parser("tag", help="Add/remove kb_tags on any md.")
    ps = p.add_subparsers(dest="tag_cmd", required=True)

    a = ps.add_parser("add", help="Append a tag.")
    a.add_argument("target", help="E.g. papers/ABCD1234, topics/X, thoughts/Y")
    a.add_argument("--tag", required=True)
    a.add_argument("--expected-mtime", type=float,
                   help="Optional mtime guard.")
    a.set_defaults(func=_cmd_tag_add)

    r = ps.add_parser("remove", help="Remove a tag.")
    r.add_argument("target")
    r.add_argument("--tag", required=True)
    r.add_argument("--expected-mtime", type=float)
    r.set_defaults(func=_cmd_tag_remove)


def _cmd_tag_add(args, ctx):
    result = tag_ops.add(
        ctx, args.target, args.tag, expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_tag_remove(args, ctx):
    result = tag_ops.remove(
        ctx, args.target, args.tag, expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


# ---------- ref ----------
def _add_ref_cmds(sub):
    p = sub.add_parser("ref", help="Add/remove kb_refs on any md.")
    ps = p.add_subparsers(dest="ref_cmd", required=True)

    a = ps.add_parser("add", help="Append a reference.")
    a.add_argument("target")
    a.add_argument("--target-ref", "--ref", dest="target_ref", required=True,
                   help="Reference to add, e.g. papers/ABCD1234.")
    a.add_argument("--expected-mtime", type=float)
    a.set_defaults(func=_cmd_ref_add)

    r = ps.add_parser("remove", help="Remove a reference.")
    r.add_argument("target")
    r.add_argument("--target-ref", "--ref", dest="target_ref", required=True)
    r.add_argument("--expected-mtime", type=float)
    r.set_defaults(func=_cmd_ref_remove)


def _cmd_ref_add(args, ctx):
    result = ref_ops.add(
        ctx, args.target, args.target_ref,
        expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_ref_remove(args, ctx):
    result = ref_ops.remove(
        ctx, args.target, args.target_ref,
        expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


# ---------- delete ----------
def _add_delete_cmd(sub):
    p = sub.add_parser(
        "delete", help="Delete a thought/topic/preference (requires --yes).",
    )
    p.add_argument("target",
                   help="E.g. thoughts/2026-04-22-x, topics/X, "
                        ".agent-prefs/writing-style")
    p.add_argument("--yes", action="store_true",
                   help="Required confirmation flag. Without it, delete "
                        "refuses to proceed.")
    p.set_defaults(func=_cmd_delete)


def _cmd_delete(args, ctx):
    if not args.yes:
        print(
            "refusing to delete without --yes. "
            "This guard prevents accidental deletion.",
            file=sys.stderr,
        )
        return 2
    result = delete_ops.delete(ctx, args.target, confirm=True)
    if args.json:
        # JSON always kb-relative: downstream scripts shouldn't
        # depend on the caller's home layout.
        print(json.dumps({
            "node_type": result.address.node_type,
            "key": result.address.key,
            "deleted": result.address.md_rel_path,
            "git_sha": result.git_sha,
            "reindexed": result.reindexed,
        }, indent=2))
    else:
        print(f"  deleted: {result.address.node_type}/{result.address.key}")
        print(f"    path: {_fmt_path(result.md_path, ctx.kb_root, absolute=args.absolute)}")
        if result.git_sha:
            print(f"    git: {result.git_sha[:12]}")
    return 0


# ---------- log ----------
def _add_log_cmd(sub):
    p = sub.add_parser(
        "log",
        help="Show recent kb-write operations from the audit log.",
    )
    p.add_argument("-n", type=_positive_int, default=20,
                   help="Number of entries to show (default 20).")
    p.add_argument("--op", help="Filter by op name (e.g. create_thought).")
    p.add_argument("--actor", help="Filter by actor (cli/mcp/python).")
    p.set_defaults(func=_cmd_log)


def _cmd_log(args, ctx):
    kb_root = _resolve_kb_root(args)
    from .audit import tail
    entries = tail(kb_root, n=max(args.n, 1) * 4)  # over-fetch for filter
    if args.op:
        entries = [e for e in entries if e.get("op") == args.op]
    if args.actor:
        entries = [e for e in entries if e.get("actor") == args.actor]
    entries = entries[-args.n:]
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("(no audit entries)")
        return 0
    for e in entries:
        ts = e.get("ts", "?")[:19]
        actor = e.get("actor", "?")
        op = e.get("op", "?")
        target = e.get("target", "?")
        sha = e.get("git_sha", "")
        sha_str = f" [git {sha[:8]}]" if sha else ""
        print(f"{ts}  {actor:>6}  {op:<20}  {target}{sha_str}")
    return 0


# ---------- rules ----------
def _add_rules_cmd(sub):
    p = sub.add_parser("rules", help="Print AGENT-WRITE-RULES.md.")
    p.set_defaults(func=_cmd_rules)


def _cmd_rules(args, ctx):
    pkg = resources.files("kb_write")
    print((pkg / "AGENT-WRITE-RULES.md").read_text(encoding="utf-8"))
    return 0


# ---------- doctor ----------
def _add_doctor_cmd(sub):
    p = sub.add_parser("doctor", help="Scan KB for rule violations.")
    p.add_argument("--fix", action="store_true",
                   help="Auto-repair safely-fixable issues.")
    p.set_defaults(func=_cmd_doctor)


def _cmd_doctor(args, ctx):
    # Doctor doesn't go through the normal ctx lifecycle (it reads
    # lots of files and may write scaffolds); it uses its own small
    # context. Build one here without lock (to allow concurrent reads
    # of same KB) and without git-commit (repairs are manual-review).
    kb_root = _resolve_kb_root(args)
    ctx = WriteContext(
        kb_root=kb_root,
        git_commit=False,
        reindex=False,
        lock=False,
    )
    report = doctor_ops.doctor(ctx, fix=args.fix)
    if args.json:
        print(json.dumps({
            "scanned": report.scanned,
            "fixed": report.fixed,
            "findings": [
                {"severity": f.severity, "category": f.category,
                 "path": f.path, "message": f.message,
                 "auto_fixable": f.auto_fixable}
                for f in report.findings
            ],
        }, indent=2))
    else:
        print(doctor_ops.format_report(report))
    return 1 if report.has_errors() else 0


# ---------- re-summarize ----------
def _add_re_summarize_cmd(sub):
    p = sub.add_parser(
        "re-summarize",
        help=(
            "Re-run the AI summariser on ONE paper and update the "
            "`## AI Summary` sections where the new LLM pass judges "
            "the new text more correct than the stored text. "
            "Preserves the 7-section structure; only section bodies "
            "change. Use when you spot errors in an existing summary."
        ),
    )
    p.add_argument(
        "target",
        help=(
            "Paper to re-summarise. Accepts: bare key 'ABCD1234', "
            "'papers/ABCD1234', 'papers/ABCD1234.md', or a "
            "book-chapter path like 'papers/BOOKKEY-ch03'. "
            "Paper must already have fulltext_processed=true "
            "(re-summarize CORRECTS existing summaries; it does "
            "not create initial ones — for that, use "
            "`kb-importer import papers --fulltext`)."
        ),
    )
    p.add_argument(
        "--provider", default=None,
        help=(
            "Override LLM provider for this run (gemini|openai|"
            "deepseek). Default: as configured in kb-importer."
        ),
    )
    p.add_argument(
        "--model", default=None,
        help="Override LLM model for this run. Default: as configured.",
    )
    p.set_defaults(func=_cmd_re_summarize)


def _cmd_re_summarize(args, ctx):
    from .ops.re_summarize import re_summarize, format_report, ReSummarizeError
    try:
        report = re_summarize(
            ctx, args.target,
            provider=args.provider,
            model=args.model,
        )
    except ReSummarizeError as e:
        print(f"re-summarize failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        # Convert absolute md_path to kb-relative for stable output;
        # absolute would leak home-dir layout via stdout → logs.
        try:
            rel = report.md_path.resolve().relative_to(
                ctx.kb_root.resolve()
            ).as_posix()
        except ValueError:
            rel = str(report.md_path)
        print(json.dumps({
            "paper_key": report.paper_key,
            "md_path": rel,
            "mtime_after": report.mtime_after,
            "git_sha": report.git_sha,
            "reindexed": report.reindexed,
            "verdicts": [
                {"section": v.section, "verdict": v.verdict,
                 "reason": v.reason}
                for v in report.verdicts
            ],
        }, indent=2))
    else:
        print(format_report(report))
    return 0


# ---------- re-read (batch re-summarize with pluggable selection) ----------
def _add_re_read_cmd(sub):
    p = sub.add_parser(
        "re-read",
        help=(
            "Batch re-summarize N papers chosen by a pluggable "
            "selector strategy. Use for periodic re-reading "
            "of the KB to surface model-improvement wins and "
            "catch stale summaries. Default picks papers never "
            "re-read before; other strategies available — see "
            "--list-selectors."
        ),
    )
    p.add_argument(
        "--count", type=_positive_int, default=5,
        help="Number of papers to re-read (default 5).",
    )
    p.add_argument(
        "--source", default="papers",
        choices=["papers", "storage"],
        help=(
            "Candidate pool. 'papers' (default): every md under "
            "papers/*.md. 'storage': only papers whose PDF is on "
            "disk under zotero_storage/ AND have an imported md."
        ),
    )
    p.add_argument(
        "--selector", default=None,
        help=(
            "Selection strategy. Default: 'unread-first'. "
            "Available: random, unread-first, stale-first, "
            "never-summarized, oldest-summary-first, by-tag, "
            "related-to-recent. Use --list-selectors for full help."
        ),
    )
    p.add_argument(
        "--selector-arg", action="append", default=[], metavar="KEY=VALUE",
        help=(
            "Key=value option forwarded to the selector. Can be "
            "repeated. Per-selector options: by-tag takes tag=<name>; "
            "related-to-recent takes anchor_days=<int>, "
            "edge_kinds=<kb_ref,citation>, fallback=<selector-name>."
        ),
    )
    p.add_argument(
        "--seed", type=int, default=None,
        help="RNG seed for reproducible selection. Default: unseeded.",
    )
    p.add_argument(
        "--dry-run-select", action="store_true",
        help=(
            "Print the N chosen papers and log dryrun events, but "
            "do NOT call any LLM or write any mds. Disjoint from "
            "the global --dry-run (which propagates into re_summarize "
            "and runs the LLM but doesn't splice). Use this to "
            "preview selection cheaply."
        ),
    )
    p.add_argument(
        "--list-selectors", action="store_true",
        help="Print all available selectors with their descriptions and exit.",
    )
    p.add_argument(
        "--provider", default=None,
        help="LLM provider override for re-summarize pass (see re-summarize --help).",
    )
    p.add_argument(
        "--model", default=None,
        help="LLM model override for re-summarize pass.",
    )
    p.set_defaults(func=_cmd_re_read)


def _cmd_re_read(args, ctx):
    from .selectors import (
        REGISTRY as SELECTOR_REGISTRY, DEFAULT_SELECTOR_NAME,
        describe_all, parse_selector_args,
    )
    from .ops.re_read import re_read, format_report

    # Handle --list-selectors fast-path (no context needed).
    if args.list_selectors:
        print(describe_all())
        return 0

    selector_name = args.selector or DEFAULT_SELECTOR_NAME
    if selector_name not in SELECTOR_REGISTRY:
        print(
            f"error: unknown selector {selector_name!r}. Available: "
            f"{', '.join(SELECTOR_REGISTRY.keys())}",
            file=sys.stderr,
        )
        return 2

    if args.count <= 0:
        print(
            f"error: --count must be positive, got {args.count}",
            file=sys.stderr,
        )
        return 2

    try:
        sel_kwargs = parse_selector_args(args.selector_arg)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Soft lint: warn about selector-arg keys the chosen selector
    # doesn't declare. Mis-spelled kwargs (e.g. `--selector-arg
    # tages=review` instead of `tag=`) would otherwise be silently
    # ignored, resulting in surprising behaviour (by-tag raising
    # "requires tag", or related-to-recent using defaults). Selectors
    # declare accepted kwargs via class attribute `ACCEPTED_KWARGS`;
    # selectors that don't declare anything accept everything (legacy
    # bypass).
    sel_obj = SELECTOR_REGISTRY[selector_name]
    accepted = getattr(sel_obj, "ACCEPTED_KWARGS", None)
    if accepted is not None and sel_kwargs:
        unknown = set(sel_kwargs) - set(accepted)
        if unknown:
            print(
                f"warning: selector {selector_name!r} doesn't recognise "
                f"args {sorted(unknown)} (accepted: {sorted(accepted)}); "
                f"did you mistype?",
                file=sys.stderr,
            )

    # For --source storage we need a storage_dir. Resolve it from
    # kb-importer's config (the canonical place it's set). If
    # kb-importer isn't installed, storage source is unavailable.
    storage_dir = None
    if args.source == "storage":
        try:
            from kb_importer.config import load_config as load_importer_config
        except ImportError:
            print(
                "error: --source storage requires kb-importer to be "
                "installed (to locate zotero_storage).",
                file=sys.stderr,
            )
            return 2
        try:
            importer_cfg = load_importer_config(kb_root=ctx.kb_root)
        except Exception as e:
            print(
                f"error: could not load kb-importer config to find "
                f"zotero_storage: {e}",
                file=sys.stderr,
            )
            return 2
        storage_dir = importer_cfg.zotero_storage_dir

    try:
        report = re_read(
            ctx,
            count=args.count,
            source_name=args.source,
            selector_name=selector_name,
            selector_args=sel_kwargs,
            seed=args.seed,
            dry_run=args.dry_run_select,
            storage_dir=storage_dir,
            provider=args.provider,
            model=args.model,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(format_report(report))
    # Non-zero exit if ANY skip happened in a non-dry-run batch —
    # so CI / cron wrappers can alarm on it.
    if not report.dry_run and report.skip_keys:
        return 1
    return 0


# ---------- helpers ----------
def _resolve_kb_root(args, *, allow_missing: bool = False) -> Path:
    try:
        root = kb_root_from_env(args.kb_root)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    if not allow_missing and not root.exists():
        print(f"error: kb_root does not exist: {root}", file=sys.stderr)
        sys.exit(2)
    return root


def _build_context(args) -> WriteContext:
    return WriteContext(
        kb_root=_resolve_kb_root(args),
        git_commit=not args.no_git_commit,
        reindex=not args.no_reindex,
        lock=not args.no_lock,
        commit_message=args.commit_message,
        dry_run=args.dry_run,
        # Stamp every CLI-originated write as actor="cli" so the
        # audit log can distinguish CLI use from MCP / Python API.
        actor="cli",
    )


def _read_body(path: Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _emit_result(args, ctx, result):
    if args.json:
        payload = {
            "node_type": result.address.node_type,
            "key": result.address.key,
            # JSON always kb-relative (stable across host layouts).
            "md_path": result.address.md_rel_path,
            "md_rel_path": result.address.md_rel_path,
            "mtime": result.mtime,
            "git_sha": result.git_sha,
            "reindexed": result.reindexed,
            "dry_run": args.dry_run,
        }
        if result.diff:
            payload["diff"] = result.diff
        if result.preview:
            payload["preview"] = result.preview
        print(json.dumps(payload, indent=2))
        return

    rendered_path = _fmt_path(
        result.md_path, ctx.kb_root, absolute=args.absolute,
    )

    if args.dry_run:
        print(f"  [dry-run] {result.address.node_type}/{result.address.key}")
        print(f"    path: {rendered_path}")
        if result.preview:
            print()
            print(result.preview)
        elif result.diff:
            print()
            print(result.diff, end="")
        else:
            print("    (no changes — write would be a no-op)")
        return

    print(f"  ✓ {result.address.node_type}/{result.address.key}")
    print(f"    path:  {rendered_path}")
    print(f"    mtime: {result.mtime:.9f}")
    if result.git_sha:
        print(f"    git:   {result.git_sha[:12]}")
    if result.reindexed:
        print(f"    reindex: ok")


# ---------- migrate-legacy-chapters ----------
def _add_migrate_legacy_chapters_cmd(sub):
    p = sub.add_parser(
        "migrate-legacy-chapters",
        help=(
            "One-shot migration of v25-style longform chapters "
            "from thoughts/<date>-<KEY>-ch<NN>-*.md to the v26 "
            "canonical location papers/<KEY>-chNN.md. Idempotent: "
            "re-running skips chapters already migrated. Preserves "
            "body content verbatim — no LLM call. Use `--dry-run` "
            "to preview the move plan."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "List the migration plan without touching any files. "
            "Disjoint from the global --dry-run flag (which would "
            "still run audit/git — this flag skips ALL filesystem "
            "writes)."
        ),
    )
    p.set_defaults(func=_cmd_migrate_legacy_chapters)


def _cmd_migrate_legacy_chapters(args, ctx):
    from .ops.migrate_chapters import (
        migrate_legacy_chapters, format_report,
    )
    report = migrate_legacy_chapters(ctx, dry_run=args.dry_run)
    print(format_report(report))
    # Non-zero exit when there were errors or unresolved collisions
    # so cron wrappers can alarm. Actual skipped-already-done is a
    # normal outcome (idempotent re-run) → rc=0.
    if report.errors or report.skipped_collision:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # Commands that don't need a WriteContext because they don't write
    # or because they build their own narrowly-scoped ctx.
    # - init: builds ctx minimally internally (it sidesteps lock on
    #         a fresh KB).
    # - rules: pure read.
    # - doctor: builds its own narrow ctx (no lock, no git).
    # - pref list/show: pure read.
    # - ai-zone show: pure read.
    no_ctx_cmds = {"init", "rules", "doctor", "log"}
    read_only_subcmds = {
        ("pref", "list"),
        ("pref", "show"),
        ("ai-zone", "show"),
    }
    ctx = None
    if args.command in no_ctx_cmds:
        ctx = None
    else:
        # Grab whatever sub-command attribute is present (varies by parent).
        sub = (
            getattr(args, "pref_cmd", None)
            or getattr(args, "ai_zone_cmd", None)
            or getattr(args, "thought_cmd", None)
            or getattr(args, "topic_cmd", None)
            or getattr(args, "tag_cmd", None)
            or getattr(args, "ref_cmd", None)
        )
        if (args.command, sub) in read_only_subcmds:
            ctx = None
        else:
            ctx = _build_context(args)

    try:
        return args.func(args, ctx)
    except RuleViolation as e:
        print(f"rule violation: {e}", file=sys.stderr)
        return 3
    except WriteConflictError as e:
        print(f"write conflict: {e}", file=sys.stderr)
        return 4
    except WriteExistsError as e:
        print(f"already exists: {e}", file=sys.stderr)
        return 5
    except ZoneError as e:
        print(f"AI-zone error: {e}", file=sys.stderr)
        return 6
    except PathError as e:
        print(f"path error: {e}", file=sys.stderr)
        return 7
    except FileNotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"unexpected error: {e!r}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
