"""`kb-write re-summarize / re-read` — LLM-driven batch ops."""
from __future__ import annotations

import json
import sys

from ._shared import _positive_int


# ---------- re-summarize ----------
def register_re_summarize(sub) -> None:
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
    from ..ops.re_summarize import re_summarize, format_report, ReSummarizeError
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
def register_re_read(sub) -> None:
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
    from ..selectors import (
        REGISTRY as SELECTOR_REGISTRY, DEFAULT_SELECTOR_NAME,
        describe_all, parse_selector_args,
    )
    from ..ops.re_read import re_read, format_report

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
