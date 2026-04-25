"""kb-citations CLI.

Subcommands:
  fetch   — hit provider APIs, cache per-paper JSON
  link    — write edges from cache into kb-mcp's links table
  status  — show cache summary
  refs    — print references for one paper (debugging)
  cites   — print citations for one paper (debugging)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kb_core.argtypes import (
    positive_int as _positive_int,
    nonnegative_int as _nonnegative_int,
)

from .cache import CitationCache
from .config import CitationsContext, kb_root_from_env, find_workspace_config
from .fetcher import build_provider, fetch_all
from .linker import link as link_step
from .resolver import LocalResolver


def _parser() -> argparse.ArgumentParser:
    from . import __version__
    p = argparse.ArgumentParser(
        prog="kb-citations",
        description=(
            "Fetch paper-to-paper citation edges from public APIs "
            "(Semantic Scholar or OpenAlex) and inject them into "
            "the ee-kb link graph."
        ),
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s v{__version__}")
    p.add_argument("--kb-root", type=Path,
                   help="KB repo root (defaults to $KB_ROOT or "
                        "workspace autodetect).")
    p.add_argument("--provider", default=None,
                   choices=["semantic_scholar", "openalex"],
                   help="Which citation API to use (default: from "
                        "kb-citations.yaml or 'semantic_scholar').")
    p.add_argument("--api-key",
                   help="S2 API key (overrides $SEMANTIC_SCHOLAR_API_KEY).")
    p.add_argument("--mailto",
                   help="Contact email for OpenAlex polite pool "
                        "(overrides $OPENALEX_MAILTO).")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON on stdout.")

    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    f = sub.add_parser("fetch", help="Fetch citation data for all "
                                     "papers with a DOI.")
    f.add_argument("--max-refs", type=_positive_int, default=1000,
                   help="Max references per paper (default 1000).")
    f.add_argument("--max-cites", type=_positive_int, default=200,
                   help="Max incoming citations per paper (default 200).")
    f.add_argument("--freshness-days", type=_nonnegative_int, default=30,
                   help="Skip papers whose cache is newer than this "
                        "(default 30). Pass 0 to force refetch.")
    f.add_argument("--with-citations", action="store_true",
                   help="Also fetch who cites each paper (not just "
                        "what it references). Doubles API cost.")
    f.add_argument("--max-api-calls", type=_positive_int, default=None,
                   help="Hard cap on provider calls this run. Default: "
                        "unlimited (rate-limited only by provider's own "
                        "throttle). Useful for sampling or when an API "
                        "key is flaky.")
    f.set_defaults(func=_cmd_fetch)

    ln = sub.add_parser("link", help="Apply cached citation data "
                                     "to kb-mcp's link graph.")
    ln.set_defaults(func=_cmd_link)

    st = sub.add_parser("status", help="Cache summary.")
    st.set_defaults(func=_cmd_status)

    r = sub.add_parser("refs", help="Print references for one paper "
                                    "(from cache).")
    r.add_argument("paper_key", help="Zotero key, e.g. ABCD1234.")
    r.set_defaults(func=_cmd_refs)

    c = sub.add_parser("cites", help="Print citations for one paper "
                                     "(from cache).")
    c.add_argument("paper_key")
    c.set_defaults(func=_cmd_cites)

    rc = sub.add_parser(
        "refresh-counts",
        help="Refresh `citation_count` for every paper in the KB "
             "(one provider GET per paper). Run periodically — "
             "citation counts grow over time.",
    )
    rc.add_argument(
        "--max-api-calls", type=_positive_int, default=None,
        help="Cap total provider calls (default: unlimited, bounded "
             "only by provider rate limits). Useful to sample or "
             "when your S2 key is flaky.",
    )
    rc.set_defaults(func=_cmd_refresh_counts)

    sg = sub.add_parser(
        "suggest",
        help="Emit a reading list of high-value dangling references — "
             "DOIs cited by multiple local papers but not in the "
             "library. Purely local (reads cache, no API).",
    )
    sg.add_argument(
        "--min-cites", type=_positive_int, default=5,
        help="Only suggest DOIs cited by at least N local papers "
             "(default 5). Lower = more candidates, more noise.",
    )
    sg.add_argument(
        "--limit", type=_positive_int, default=50,
        help="Max DOIs to emit (default 50).",
    )
    sg.add_argument(
        "--format", choices=["text", "ris", "bibtex", "json"],
        default="text",
        help="text (human), ris/bibtex (import into Zotero), or json.",
    )
    sg.set_defaults(func=_cmd_suggest)

    return p


def _build_ctx(args) -> CitationsContext:
    try:
        kb_root = kb_root_from_env(args.kb_root)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    # Load workspace YAML if present (`.ee-kb-tools/config/kb-citations.yaml`).
    # YAML provides defaults; CLI args and env vars override.
    yaml_cfg: dict = {}
    cfg_path = find_workspace_config()
    if cfg_path is not None:
        try:
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if loaded is None:
                yaml_cfg = {}
            elif isinstance(loaded, dict):
                yaml_cfg = loaded
            else:
                print(
                    f"warning: {cfg_path} has a {type(loaded).__name__} "
                    f"at the top level, but a mapping is required. "
                    f"Ignoring config.",
                    file=sys.stderr,
                )
                yaml_cfg = {}
        except Exception as e:
            print(f"warning: could not read {cfg_path}: {e}",
                  file=sys.stderr)

    def _from_yaml(key, default):
        return yaml_cfg.get(key, default)

    # API key: CLI --api-key > env > YAML api_key_env var.
    api_key = getattr(args, "api_key", None)
    if not api_key:
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if not api_key:
        key_env_name = yaml_cfg.get("api_key_env")
        if key_env_name:
            api_key = os.environ.get(key_env_name)

    # mailto: CLI > env > YAML.
    mailto = getattr(args, "mailto", None)
    if not mailto:
        mailto = os.environ.get("OPENALEX_MAILTO") or yaml_cfg.get("mailto")

    return CitationsContext(
        kb_root=kb_root,
        provider=args.provider or _from_yaml("provider", "semantic_scholar"),
        api_key=api_key,
        mailto=mailto,
        max_refs=getattr(args, "max_refs", None) or _from_yaml("max_refs", 1000),
        max_cites=getattr(args, "max_cites", None) or _from_yaml("max_cites", 200),
        freshness_days=(
            None if getattr(args, "freshness_days", None) == 0
            else getattr(args, "freshness_days", None)
                 or _from_yaml("freshness_days", 30)
        ),
        fetch_citations=(
            getattr(args, "with_citations", False)
            or _from_yaml("fetch_citations", False)
        ),
    )


# ------------- fetch -------------
def _record_citations_run(
    kb_root, *, subcommand: str, rc: int,
    ok_count: int, err_count: int,
    extra: dict | None = None,
) -> None:
    """Emit a CITATIONS_RUN event summarising one kb-citations
    subcommand (fetch / link / refresh-counts). Best-effort — never
    raises; logging failure never affects the command's exit code.

    `rc` is the command's return code. `ok_count` / `err_count` are
    the subcommand's own tallies (papers fetched vs fetch_errors,
    etc.). `subcommand` identifies which CLI was run; it lands in
    `extra.subcommand` for the periodic report to aggregate per
    subcommand.
    """
    try:
        from kb_importer.events import (
            record_event, EVENT_CITATIONS_RUN,
            CITATIONS_RUN_OK, CITATIONS_RUN_PARTIAL, CITATIONS_RUN_ABORTED,
        )
    except ImportError:
        return
    try:
        if rc == 0 and err_count == 0:
            category = CITATIONS_RUN_OK
        elif ok_count == 0 and err_count > 0:
            category = CITATIONS_RUN_ABORTED
        else:
            category = CITATIONS_RUN_PARTIAL
        merged_extra = {"subcommand": subcommand}
        if extra:
            merged_extra.update(extra)
        record_event(
            kb_root,
            event_type=EVENT_CITATIONS_RUN,
            category=category,
            detail=f"{subcommand}: ok={ok_count} err={err_count}",
            pipeline="citations",
            extra=merged_extra,
        )
    except Exception:
        pass


def _cmd_fetch(args):
    ctx = _build_ctx(args)
    try:
        provider = build_provider(ctx)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        report = fetch_all(
            ctx, provider,
            max_api_calls=getattr(args, "max_api_calls", None),
        )
    finally:
        try:
            provider.close()
        except Exception:
            pass

    if args.json:
        print(json.dumps({
            "kb_root": str(ctx.kb_root),
            "provider": ctx.provider,
            "total_papers": report.total_papers,
            "skipped_no_doi": report.skipped_no_doi,
            "skipped_fresh_cache": report.skipped_fresh_cache,
            "fetched": report.fetched,
            "fetch_errors": report.fetch_errors,
            "total_references_collected": report.total_references_collected,
            "total_citations_collected": report.total_citations_collected,
        }, indent=2))
    else:
        print(
            "\nnext steps:\n"
            "  kb-citations link              # push edges to kb-mcp\n"
            "  kb-citations refresh-counts    # populate citation_count\n"
            "                                 #   on each paper (one GET each)",
            file=sys.stderr,
        )
    rc = 0 if report.fetch_errors == 0 else 1
    _record_citations_run(
        ctx.kb_root, subcommand="fetch", rc=rc,
        ok_count=report.fetched, err_count=report.fetch_errors,
        extra={
            "provider":       ctx.provider,
            "total_papers":   report.total_papers,
            "skipped_no_doi": report.skipped_no_doi,
            "refs_collected": report.total_references_collected,
        },
    )
    return rc


# ------------- link -------------
def _cmd_link(args):
    ctx = _build_ctx(args)
    report = link_step(ctx.kb_root)

    # Collapse all return paths to a single `rc = ...; break; emit; return`
    # so the CITATIONS_RUN event is written exactly once no matter
    # which branch fires (json path / db-write success / fallback /
    # total failure).
    if args.json:
        print(json.dumps({
            "cached_papers_scanned": report.cached_papers_scanned,
            "edges_emitted": report.edges_emitted,
            "edges_to_dangling": report.edges_to_dangling,
            "db_updated": report.db_updated,
            "db_error": report.db_error,
            "fallback_file": str(report.fallback_file) if report.fallback_file else None,
            "unresolved_samples": report.unresolved_samples,
        }, indent=2))
        rc = 0 if (report.db_updated or report.fallback_file) else 1
    else:
        print(f"kb-citations link: scanned {report.cached_papers_scanned} "
              f"cached papers")
        print(f"  edges emitted (in-KB): {report.edges_emitted}")
        print(f"  refs to papers not in KB: {report.edges_to_dangling}")
        if report.db_updated:
            print(f"  ✓ kb-mcp links table updated "
                  f"(origin=citation rows replaced)")
            rc = 0
        elif report.fallback_file:
            print(f"  ⚠ kb-mcp unavailable; wrote JSONL to "
                  f"{report.fallback_file}")
            print(f"    ({report.db_error})")
            rc = 0   # graceful fallback is still a "ran ok" from link's POV
        elif report.db_error and report.edges_emitted == 0:
            # v0.28.2: no edges to write + DB unavailable = nothing
            # to do. Previously we'd say "✗ link failed" even though
            # there was no work and the user was probably just
            # checking. Report as info, exit 0.
            print(
                f"  i no edges to write (scanned "
                f"{report.cached_papers_scanned} cached papers); "
                f"kb-mcp DB unavailable but not needed"
            )
            print(f"    ({report.db_error})")
            rc = 0
        else:
            print(f"  ✗ link failed: {report.db_error}")
            rc = 1
        if report.unresolved_samples:
            print("\n  sample unresolved references (first 10):")
            for s in report.unresolved_samples:
                doi = s.get("ref_doi") or "(no DOI)"
                title = (s.get("ref_title") or "")[:60]
                print(f"    {s['src']} → {doi}  {title}")

    _record_citations_run(
        ctx.kb_root, subcommand="link", rc=rc,
        ok_count=report.edges_emitted,
        err_count=(0 if rc == 0 else 1),
        extra={
            "scanned":     report.cached_papers_scanned,
            "to_dangling": report.edges_to_dangling,
            "db_updated":  bool(report.db_updated),
            "fallback":    bool(report.fallback_file),
        },
    )
    return rc


# ------------- status -------------
def _cmd_status(args):
    ctx = _build_ctx(args)
    cache = CitationCache(ctx.kb_root)
    summary = cache.summary()
    resolver = LocalResolver.from_kb(ctx.kb_root)
    summary["local_papers_total"] = len(resolver)
    summary["local_papers_with_doi"] = len(resolver.papers_with_doi)
    summary["kb_root"] = str(ctx.kb_root)

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"kb-citations status: {ctx.kb_root}")
    print(f"  local papers: {summary['local_papers_total']} "
          f"({summary['local_papers_with_doi']} with DOI)")
    print(f"  cached: {summary['cached_papers']}")
    print(f"  references collected: "
          f"{summary['total_references_fetched']}")
    print(f"  citations collected: "
          f"{summary['total_citations_fetched']}")
    if summary["by_provider"]:
        print("  by provider:")
        for prov, n in summary["by_provider"].items():
            print(f"    {prov}: {n}")
    return 0


# ------------- refs / cites -------------
def _cmd_refs(args):
    return _dump_one(args, "references")


def _cmd_cites(args):
    return _dump_one(args, "citations")


def _cmd_suggest(args) -> int:
    """Aggregate dangling references across the cache and emit as
    text / RIS / BibTeX / JSON.

    This reads ONLY the cache (cached_papers_scanned(kb_root)/...json),
    compares DOIs against local papers via a sibling kb_mcp projection
    if available (else falls back to markdown frontmatter scan — slow
    but works without kb_mcp installed).

    RIS / BibTeX outputs are ready for Zotero's "Import from
    Clipboard" — pipe the output to `xclip -selection clipboard`
    (Linux) or `pbcopy` (macOS) and hit Cmd/Ctrl-Shift-Alt-I in
    Zotero.
    """
    from .resolver import LocalResolver
    ctx = _build_ctx(args)
    cache = CitationCache(ctx.kb_root)
    keys = cache.all_keys()
    if not keys:
        print("(no citation cache — run `kb-citations fetch` first)",
              file=sys.stderr)
        return 1

    resolver = LocalResolver.from_kb(ctx.kb_root)
    local_dois = {
        p.doi for p in resolver if p.doi
    }

    # Aggregate.
    counts: dict[str, int] = {}
    meta: dict[str, dict] = {}
    for k in keys:
        data = cache.load(k)
        if not data:
            continue
        for ref in data.get("references") or []:
            doi = (ref.get("doi") or "").strip().lower()
            if not doi or doi in local_dois:
                continue
            counts[doi] = counts.get(doi, 0) + 1
            if doi not in meta:
                authors = ref.get("authors") or []
                meta[doi] = {
                    "title": ref.get("title") or "",
                    "year": ref.get("year"),
                    "authors": authors,
                }

    filtered = sorted(
        ((doi, n) for doi, n in counts.items() if n >= args.min_cites),
        key=lambda x: -x[1],
    )[:args.limit]

    if not filtered:
        total = sum(1 for n in counts.values() if n >= 1)
        print(
            f"(no DOIs cited by >= {args.min_cites} local papers; "
            f"{total} distinct dangling DOIs exist — lower "
            "--min-cites to see them)",
            file=sys.stderr,
        )
        return 1

    if args.format == "json":
        out = [
            {
                "doi": doi,
                "cited_by_local_count": n,
                "title": meta.get(doi, {}).get("title"),
                "year": meta.get(doi, {}).get("year"),
                "authors": meta.get(doi, {}).get("authors", []),
            }
            for doi, n in filtered
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.format == "ris":
        for doi, n in filtered:
            m = meta.get(doi, {})
            # Minimal valid RIS record.
            print("TY  - JOUR")
            if m.get("title"):
                print(f"TI  - {m['title']}")
            for author in m.get("authors") or []:
                print(f"AU  - {author}")
            if m.get("year"):
                print(f"PY  - {m['year']}")
            print(f"DO  - {doi}")
            print(f"N1  - Cited by {n} papers in ee-kb; "
                  "imported via kb-citations suggest")
            print("ER  -")
            print()
        return 0

    if args.format == "bibtex":
        for i, (doi, n) in enumerate(filtered, start=1):
            m = meta.get(doi, {})
            # Cite key from first author's last name + year, fallback
            # to doi-derived.
            first_author = ""
            authors = m.get("authors") or []
            if authors:
                # Rough: last word of first author.
                first_author = authors[0].split()[-1].lower()
            cite_key = (
                f"{first_author}{m.get('year') or ''}"
                if first_author else
                f"dangling{i}"
            )
            # Escape braces in title.
            title = (m.get("title") or "").replace("{", "").replace("}", "")
            print(f"@article{{{cite_key},")
            if title:
                print(f"  title = {{{title}}},")
            if authors:
                print(f"  author = {{{' and '.join(authors)}}},")
            if m.get("year"):
                print(f"  year = {{{m['year']}}},")
            print(f"  doi = {{{doi}}},")
            print(f"  note = {{Cited by {n} papers in ee-kb}},")
            print("}")
            print()
        return 0

    # Default: text.
    print(f"kb-citations suggest: top {len(filtered)} dangling "
          f"references (cited by >= {args.min_cites} local papers):\n")
    for doi, n in filtered:
        m = meta.get(doi, {})
        title = (m.get("title") or "")[:70]
        year = m.get("year") or "????"
        authors_list = m.get("authors") or []
        author = (authors_list[0] if authors_list else "")[:25]
        print(
            f"  {n:>3}×  {doi:<42}  [{year}] {author:<26}  {title}"
        )
    total_dangling = len(counts)
    if total_dangling > len(filtered):
        print(
            f"\n({total_dangling - len(filtered)} more dangling DOIs at "
            f"lower frequency; lower --min-cites or raise --limit)"
        )
    return 0


def _cmd_refresh_counts(args) -> int:
    from .counts_writer import refresh_counts
    ctx = _build_ctx(args)
    try:
        provider = build_provider(ctx)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        report = refresh_counts(
            ctx, provider,
            max_api_calls=args.max_api_calls,
        )
    except FileNotFoundError as e:
        # Projection DB missing. Prior versions let the raw Python
        # traceback leak through, which read as a crash. `link` has
        # handled the same situation gracefully since v22 (soft
        # fallback to JSONL); here we can't silently continue — a
        # citation-count write needs the DB — but we can at least
        # match the UX of `link` with a clear message + pointer.
        print(
            f"kb-citations refresh-counts: cannot update citation "
            f"counts: {e}\n"
            f"  Run `kb-mcp index` first to build the projection "
            f"DB, then retry.",
            file=sys.stderr,
        )
        try:
            provider.close()
        except Exception:
            pass
        return 2
    except RuntimeError as e:
        # kb_mcp not installed — the `from kb_mcp.citation_ops ...`
        # soft import failed. Also user-fixable, so format similarly.
        print(
            f"kb-citations refresh-counts: {e}\n"
            f"  Install kb-mcp (`pip install -e kb_mcp/`) and retry.",
            file=sys.stderr,
        )
        try:
            provider.close()
        except Exception:
            pass
        return 2
    finally:
        # `provider.close()` is idempotent; the early-return branches
        # above close too, but belt-and-suspenders since forgetting
        # would leak an httpx Client per invocation.
        try:
            provider.close()
        except Exception:
            pass

    if args.json:
        print(json.dumps({
            "total_papers": report.total_papers,
            "updated": report.updated,
            "skipped_no_doi": report.skipped_no_doi,
            "not_in_provider": report.not_in_provider,
            "fetch_errors": report.fetch_errors,
            "db_write_errors": report.db_write_errors,
        }, indent=2))

    rc = 0 if (report.fetch_errors == 0 and report.db_write_errors == 0) else 1
    _record_citations_run(
        ctx.kb_root, subcommand="refresh-counts", rc=rc,
        ok_count=report.updated,
        err_count=report.fetch_errors + report.db_write_errors,
        extra={
            "provider":        ctx.provider,
            "total_papers":    report.total_papers,
            "skipped_no_doi":  report.skipped_no_doi,
            "not_in_provider": report.not_in_provider,
        },
    )
    return rc


def _dump_one(args, which: str) -> int:
    ctx = _build_ctx(args)
    cache = CitationCache(ctx.kb_root)
    data = cache.load(args.paper_key)
    if not data:
        print(f"no cache for {args.paper_key}", file=sys.stderr)
        return 1
    refs = data.get(which) or []
    if args.json:
        print(json.dumps(refs, indent=2, ensure_ascii=False))
        return 0
    print(f"{args.paper_key}: {len(refs)} {which}")
    for i, r in enumerate(refs, 1):
        doi = r.get("doi") or "(no DOI)"
        yr = r.get("year") or "????"
        title = (r.get("title") or "")[:80]
        print(f"  {i:3d}. [{yr}] {doi}  {title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"unexpected error: {e!r}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
