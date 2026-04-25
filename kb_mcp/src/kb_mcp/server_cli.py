"""kb-mcp CLI: argparse wiring + subcommand impls that don't need
the running MCP server.

Extracted from server.py in v0.28.0. Three groups of code:

- `_positive_int`, `_setup_logging`: small helpers.
- `build_parser()`: the full argparse tree (index / reindex /
  similarity-prior-save / similarity-prior-compare / snapshot /
  index-status / report). Extracted verbatim.
- `_cmd_*_impl`: the three citation-related subcommand
  implementations (fetch/link/refresh-counts). Also invoked by
  the matching MCP tool wrappers in server.py — those call the
  functions here with `_kb_root()` injected.

Why this file exists: server.py was 2100+ lines; the argparse
builder alone was 165 lines of boilerplate. Keeping it here lets
server.py focus on MCP tool surface + main() glue.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from kb_core.argtypes import positive_int as _positive_int  # noqa: F401

from .embedding import SUPPORTED_PROVIDERS


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    p = argparse.ArgumentParser(
        prog="kb-mcp",
        description="Projection + MCP server for the ee-kb knowledge base.",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s v{__version__}")
    p.add_argument("--kb-root", type=Path,
                   help="KB repo root (overrides config/env).")
    p.add_argument("--config", type=Path,
                   help="Config file (overrides default).")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    # `serve` is the default (run without a subcommand).
    sub.add_parser("serve", help="Run the MCP stdio server (default).")

    p_idx = sub.add_parser("index", help="Refresh the projection DB.")
    p_idx.add_argument(
        "--full", action="store_true",
        help="(future) Force full rebuild. Currently same as default "
             "because mtime comparison already makes incremental free.",
    )
    p_idx.add_argument(
        "--only-key", type=str, default=None,
        help="Restrict to these stems (comma-separated, e.g. "
             "AAAAAAAA,BBBBBBBB). Used to pilot-test a handful of "
             "papers without touching the rest of the index. Orphan "
             "removal is skipped in this mode.",
    )
    p_idx.add_argument(
        "--filter", dest="path_glob", type=str, default=None,
        help="Restrict to md files whose kb-root-relative path "
             "matches this glob (e.g. 'papers/HR*' or 'thoughts/*'). "
             "Combined with --only-key via union, not intersection.",
    )

    p_rei = sub.add_parser(
        "reindex",
        help="Nuke the projection DB and rebuild from scratch. Use when "
             "switching embedding provider, changing model, or when the "
             "DB is corrupt. Requires --force since this is destructive.",
    )
    p_rei.add_argument(
        "--force", action="store_true",
        help="Required. Confirms you want to delete index.sqlite and "
             "rebuild.",
    )
    p_rei.add_argument(
        "--provider", default=None,
        choices=list(SUPPORTED_PROVIDERS),
        help="Override embeddings.provider for this rebuild. "
             "Choices: openai, gemini, openrouter.",
    )
    p_rei.add_argument(
        "--model", default=None,
        help="Override embeddings.model for this rebuild. Examples: "
             "text-embedding-3-large (openai), "
             "openai/text-embedding-3-small (openrouter), "
             "gemini-embedding-001 (gemini).",
    )
    p_rei.add_argument(
        "--dim", type=_positive_int, default=None,
        help="Override embeddings.dim (vec0 table rebuilds to match).",
    )

    p_sp_save = sub.add_parser(
        "similarity-prior-save",
        help="Extract a model-agnostic top-K similarity prior from the "
             "current vector index and save to "
             "ee-kb/.kb-mcp/similarity-prior.json. Run this BEFORE "
             "changing embedding model — the prior lets you verify "
             "the new model's neighbors roughly match the old one's.",
    )
    p_sp_save.add_argument(
        "--top-k", type=_positive_int, default=20,
        help="Neighbors per paper to record (default 20).",
    )

    p_sp_cmp = sub.add_parser(
        "similarity-prior-compare",
        help="Compare current vector index against the saved prior. "
             "Reports mean Jaccard of top-K neighbor sets; low values "
             "mean the new embedding disagrees with the old about "
             "which papers are close.",
    )
    p_sp_cmp.add_argument(
        "--at-k", type=_positive_int, default=10,
        help="Top-K depth to compare at (default 10).",
    )

    p_snap = sub.add_parser(
        "snapshot",
        help="Export or import the projection DB + caches as a tar. "
             "Covers ee-kb/.kb-mcp/{index.sqlite, citations/, "
             "similarity-prior.json}. Does NOT cover md files (git) "
             "or PDFs (rsync/Zotero sync).",
    )
    snap_sub = p_snap.add_subparsers(dest="snapshot_action",
                                      metavar="ACTION")
    p_snap_exp = snap_sub.add_parser(
        "export",
        help="Write a tar snapshot to the given path. Uses "
             "VACUUM INTO for DB consistency. Suffix .tar.gz to "
             "compress.",
    )
    p_snap_exp.add_argument("path", type=Path, help="Output tar file.")

    p_snap_imp = snap_sub.add_parser(
        "import",
        help="Restore a tar snapshot into the current KB. Refuses to "
             "overwrite index.sqlite without --force.",
    )
    p_snap_imp.add_argument("path", type=Path, help="Input tar file.")
    p_snap_imp.add_argument(
        "--force", action="store_true",
        help="Overwrite existing index.sqlite if present.",
    )

    p_ix_status = sub.add_parser(
        "index-status",
        help="Print projection DB stats and staleness (offline variant "
             "of the MCP tool).",
    )
    p_ix_status.add_argument(
        "--deep", action="store_true",
        help="Additionally run `PRAGMA integrity_check` against the "
             "DB file. Catches bit-rot / partial writes / filesystem "
             "corruption that ordinary queries don't surface — a "
             "single-byte flip can leave all reads succeeding while "
             "the index is silently wrong. Slow (scans every page); "
             "use on-demand, not for routine status checks.",
    )

    p_report = sub.add_parser(
        "report",
        help="Periodic operational digest (skip events, re-read "
             "outcomes, future sections). Reads "
             "<kb_root>/.kb-mcp/events.jsonl.",
    )
    p_report.add_argument(
        "--days", type=_positive_int, default=30,
        help="Window size in days (default 30). Ignored if --since given.",
    )
    p_report.add_argument(
        "--since", type=str, default=None,
        help="ISO date/time lower bound (e.g. 2026-04-01 or "
             "2026-04-01T00:00:00Z). Overrides --days.",
    )
    p_report.add_argument(
        "--sections", type=str, default=None,
        help="Comma-separated section names. Default: all registered. "
             "Current: ops, skip, re_read, re_summarize, orphans.",
    )
    p_report.add_argument(
        "--include-normal", action="store_true",
        help="Include 'normal' skip categories (already_processed). "
             "Off by default — normal skips aren't problems.",
    )
    p_report.add_argument(
        "--out", type=Path, default=None,
        help="Write report to file instead of stdout (markdown).",
    )
    return p


# ---------------------------------------------------------------------
# Citation subcommand implementations
#
# These are called from two sites:
#   1. The matching @mcp.tool() wrappers in server.py (fetch_citations
#      / link_citations / refresh_citation_counts) — the agent-visible
#      surface. Those pass `_kb_root()` from the running server.
#   2. main() in server.py for CLI use.
# Extracted from server.py in v0.28.0. Signatures changed to take
# `kb_root` explicitly rather than reaching into server.py's
# module-level `_cfg`; the circular-import otherwise required feels
# worse than an extra positional arg.
# ---------------------------------------------------------------------

def _cmd_fetch_citations_impl(
    kb_root: Path,
    paper_keys: list[str] | None,
    provider: str | None,
    with_incoming: bool,
    max_api_calls: int | None,
) -> str:
    try:
        from kb_citations.config import CitationsContext
        from kb_citations.fetcher import build_provider, fetch_all
    except ImportError as e:
        return f"error: kb-citations not installed ({e})"

    ctx = CitationsContext(
        kb_root=kb_root,
        provider=provider or "semantic_scholar",
        fetch_citations=with_incoming,
    )
    try:
        prov = build_provider(ctx)
    except ValueError as e:
        return f"error: {e}"

    messages: list[str] = []
    def _log(s: str):
        messages.append(s)

    try:
        report = fetch_all(
            ctx, prov,
            progress=_log,
            max_api_calls=max_api_calls,
            only_keys=paper_keys,
        )
    except Exception as e:
        return f"fetch error: {e}"
    finally:
        try:
            prov.close()
        except Exception:
            pass

    summary = (
        f"fetched: {report.fetched}\n"
        f"cached-skip: {report.skipped_fresh_cache}\n"
        f"no-DOI-skip: {report.skipped_no_doi}\n"
        f"fetch-errors: {report.fetch_errors}\n"
        f"references collected: {report.total_references_collected}\n"
        f"citations collected:  {report.total_citations_collected}\n\n"
        "next: call link_citations to apply edges; optionally "
        "refresh_citation_counts to update papers.citation_count."
    )
    # Include last ~10 progress lines for observability.
    if messages:
        summary += "\n\nprogress:\n" + "\n".join(messages[-10:])
    return summary


def _cmd_link_citations_impl(kb_root: Path) -> str:
    try:
        from kb_citations.linker import link as link_step
    except ImportError as e:
        return f"error: kb-citations not installed ({e})"

    try:
        report = link_step(kb_root)
    except Exception as e:
        return f"link error: {e}"

    lines = [
        f"cached papers scanned: {report.cached_papers_scanned}",
        f"edges emitted:         {report.edges_emitted}",
        f"edges to dangling:     {report.edges_to_dangling}",
    ]
    if report.db_updated:
        lines.append("status: wrote to kb-mcp links table")
    elif report.fallback_file:
        lines.append(
            f"status: DB unavailable ({report.db_error}); wrote JSONL "
            f"fallback at {report.fallback_file}"
        )
    else:
        lines.append("status: nothing to write")
    if report.unresolved_samples:
        lines.append("\nsample unresolved references:")
        for s in report.unresolved_samples[:5]:
            lines.append(
                f"  {s['src']} → {s.get('ref_doi', '(no doi)')}  "
                f"{(s.get('ref_title') or '')[:60]}"
            )
    return "\n".join(lines)


def _cmd_refresh_counts_impl(
    kb_root: Path,
    paper_keys: list[str] | None,
    provider: str | None,
    max_api_calls: int | None,
) -> str:
    try:
        from kb_citations.config import CitationsContext
        from kb_citations.fetcher import build_provider
        from kb_citations.counts_writer import refresh_counts
    except ImportError as e:
        return f"error: kb-citations not installed ({e})"

    ctx = CitationsContext(
        kb_root=kb_root,
        provider=provider or "semantic_scholar",
    )
    try:
        prov = build_provider(ctx)
    except ValueError as e:
        return f"error: {e}"

    messages: list[str] = []
    def _log(s: str):
        messages.append(s)

    try:
        report = refresh_counts(
            ctx, prov,
            progress=_log,
            max_api_calls=max_api_calls,
            only_keys=paper_keys,
        )
    except Exception as e:
        return f"refresh error: {e}"
    finally:
        try:
            prov.close()
        except Exception:
            pass

    summary = (
        f"updated: {report.updated}\n"
        f"not-in-provider: {report.not_in_provider}\n"
        f"no-DOI-skip: {report.skipped_no_doi}\n"
        f"fetch-errors: {report.fetch_errors}\n"
        f"DB-write-errors: {report.db_write_errors}"
    )
    if messages:
        summary += "\n\nprogress:\n" + "\n".join(messages[-10:])
    return summary
