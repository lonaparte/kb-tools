"""`kb-write re-read` — batch re-summarisation.

Picks N papers via a pluggable Selector from a configurable Source,
then runs `re_summarize()` on each. Every outcome (success or
skip) is written to `<kb_root>/.kb-mcp/events.jsonl` with
event_type=re_read so `kb-mcp report` can aggregate.

Reuses the v26 re_summarize implementation — re-read is the batch
+ auto-selection layer on top of it. No new LLM / PDF code.

Flow per batch:

  1. Materialise candidate pool from --source.
  2. Ask selector.select(...) for N paper_keys.
  3. For each chosen paper_key:
     - Dry run → print + log RE_READ_DRYRUN event + continue.
     - Real run → call re_summarize; classify outcome; log event.
  4. Print summary line with counts.

re-read does NOT enforce fulltext_processed=true (re_summarize
does — re-read is just a batch dispatcher). If a paper has no
fulltext yet, re_summarize will raise ReSummarizeError and we'll
record the skip.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import WriteContext
from ..selectors import (
    PaperInfo, REGISTRY as SELECTOR_REGISTRY, DEFAULT_SELECTOR_NAME,
)
from .re_read_sources import source_papers, source_storage, SOURCES


log = logging.getLogger(__name__)


@dataclass
class ReReadReport:
    selector_name: str
    source_name: str
    count_requested: int
    count_selected: int
    chosen_keys: list[str] = field(default_factory=list)
    success_keys: list[str] = field(default_factory=list)
    skip_keys: list[tuple[str, str]] = field(default_factory=list)  # (key, reason)
    total_sections_updated: int = 0
    dry_run: bool = False


def re_read(
    ctx: WriteContext,
    *,
    count: int = 5,
    source_name: str = "papers",
    selector_name: str = DEFAULT_SELECTOR_NAME,
    selector_args: dict | None = None,
    seed: int | None = None,
    dry_run: bool = False,
    storage_dir: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> ReReadReport:
    """Entry point. See module docstring for semantics.

    Args:
        ctx: WriteContext (kb_root, git_commit, reindex, lock, etc.)
        count: how many papers to re-read this batch.
        source_name: "papers" (default) or "storage".
        selector_name: one of selectors.REGISTRY keys.
        selector_args: dict[str, str] passed through to the selector
                       (parsed from --selector-arg key=value pairs).
        seed: RNG seed for reproducibility.
        dry_run: if True, print which papers would be re-read, log
                 dryrun events, but don't run any LLM.
        storage_dir: only used when source_name == "storage".
        provider / model: forwarded to re_summarize per-paper.

    Raises:
        ValueError: unknown source or selector name.
    """
    kb_root = ctx.kb_root

    # --- source ---
    if source_name not in SOURCES:
        raise ValueError(
            f"unknown source {source_name!r}; available: "
            f"{', '.join(SOURCES.keys())}"
        )
    if source_name == "papers":
        pool = source_papers(kb_root)
    elif source_name == "storage":
        if storage_dir is None:
            raise ValueError(
                "source 'storage' requires storage_dir to be passed "
                "(usually auto-detected from kb-importer config)."
            )
        pool = source_storage(kb_root, storage_dir)
    else:  # pragma: no cover — guarded by the check above
        raise ValueError(f"source {source_name!r} not implemented")

    # --- selector ---
    sel = SELECTOR_REGISTRY.get(selector_name)
    if sel is None:
        raise ValueError(
            f"unknown selector {selector_name!r}; available: "
            f"{', '.join(SELECTOR_REGISTRY.keys())}"
        )

    chosen = sel.select(
        pool,
        count=count,
        kb_root=kb_root,
        seed=seed,
        **(selector_args or {}),
    )

    report = ReReadReport(
        selector_name=selector_name,
        source_name=source_name,
        count_requested=count,
        count_selected=len(chosen),
        chosen_keys=list(chosen),
        dry_run=dry_run,
    )

    # Import events lazily — kb_write doesn't hard-depend on kb_importer.
    # If kb_importer isn't installed, we still do the work; we just
    # can't record events. Warn once.
    try:
        from kb_importer.events import (
            record_event, EVENT_RE_READ,
            RE_READ_SUCCESS, RE_READ_SKIP_LLM,
            RE_READ_SKIP_MTIME, RE_READ_SKIP_PDF,
            RE_READ_SKIP_NOT_PROCESSED, RE_READ_DRYRUN,
        )
        _events_ok = True
    except ImportError:
        log.warning(
            "kb_importer not installed — re-read events will not be "
            "recorded. Install kb_importer for periodic aggregation."
        )
        _events_ok = False

    # --- dry-run path ---
    if dry_run:
        print(f"[dry-run] would re-read {len(chosen)} paper(s) via "
              f"selector={selector_name}, source={source_name}:",
              file=sys.stderr)
        for k in chosen:
            print(f"  - {k}", file=sys.stderr)
            if _events_ok:
                record_event(
                    kb_root,
                    event_type=EVENT_RE_READ,
                    paper_key=k, category=RE_READ_DRYRUN,
                    detail="dry-run preview",
                    extra={"selector": selector_name, "source": source_name},
                )
        return report

    # --- real runs ---
    from .re_summarize import re_summarize, ReSummarizeError

    for k in chosen:
        try:
            rs_report = re_summarize(
                ctx, f"papers/{k}",
                provider=provider, model=model,
            )
            # success: count it; add section update count
            n_new = sum(
                1 for v in rs_report.verdicts if v.verdict == "new"
            )
            report.success_keys.append(k)
            report.total_sections_updated += n_new
            if _events_ok:
                record_event(
                    kb_root,
                    event_type=EVENT_RE_READ,
                    paper_key=k, category=RE_READ_SUCCESS,
                    detail=f"{n_new} of {len(rs_report.verdicts)} sections updated",
                    provider=provider, model_tried=model,
                    pipeline="re_read",
                    extra={
                        "selector": selector_name,
                        "source": source_name,
                        "sections_updated": n_new,
                    },
                )
            print(
                f"  ✓ {k}  updated {n_new} section(s)"
                + (f"  [{rs_report.git_sha[:8]}]" if rs_report.git_sha else ""),
                file=sys.stderr,
            )
        except ReSummarizeError as e:
            # Classify into a RE_READ_* category.
            #
            # v27: prefer the exception's structured `code` attribute
            # (set at the raise site); fall back to substring matching
            # for ReSummarizeError instances that pre-date the
            # `code=` parameter or come from external callers. This
            # means a provider changing its 400 wording no longer
            # re-routes events from `bad_request` to `llm_other`.
            code = getattr(e, "code", None)
            msg = str(e)
            if code == "not_processed":
                cat = RE_READ_SKIP_NOT_PROCESSED if _events_ok else "skip_not_processed"
            elif code == "pdf_missing":
                cat = RE_READ_SKIP_PDF if _events_ok else "skip_pdf_missing"
            elif code == "mtime_conflict":
                cat = RE_READ_SKIP_MTIME if _events_ok else "skip_mtime_conflict"
            elif code in ("bad_request", "llm_other", "quota", None):
                # Fall back to substring classification when no code
                # is set. This preserves v26 behaviour for old call
                # sites while new raise sites get deterministic
                # routing via code.
                if code is not None:
                    cat = RE_READ_SKIP_LLM if _events_ok else "skip_llm_error"
                else:
                    low = msg.lower()
                    if "fulltext_processed is not true" in low or "not processed" in low:
                        cat = RE_READ_SKIP_NOT_PROCESSED if _events_ok else "skip_not_processed"
                    elif "pdf" in low and ("missing" in low or "not found" in low or "no pdf" in low):
                        cat = RE_READ_SKIP_PDF if _events_ok else "skip_pdf_missing"
                    elif "mtime" in low or "conflict" in low:
                        cat = RE_READ_SKIP_MTIME if _events_ok else "skip_mtime_conflict"
                    else:
                        cat = RE_READ_SKIP_LLM if _events_ok else "skip_llm_error"
            else:
                # Unknown code — log as generic LLM skip so nothing
                # slips through silently, and future codes get a
                # conservative default.
                cat = RE_READ_SKIP_LLM if _events_ok else "skip_llm_error"
            report.skip_keys.append((k, msg[:200]))
            if _events_ok:
                record_event(
                    kb_root,
                    event_type=EVENT_RE_READ,
                    paper_key=k, category=cat, detail=msg,
                    provider=provider, model_tried=model,
                    pipeline="re_read",
                    extra={
                        "selector": selector_name,
                        "source": source_name,
                    },
                )
            print(f"  ✗ {k}  skipped: {msg}", file=sys.stderr)
        except Exception as e:
            # Unexpected — log but don't crash the batch. Leaving N-1
            # papers un-processed because of one infra hiccup on paper
            # #3 is a worse outcome than continuing.
            log.exception("unexpected failure in re-read for %s", k)
            report.skip_keys.append((k, f"{type(e).__name__}: {e}"))
            if _events_ok:
                record_event(
                    kb_root,
                    event_type=EVENT_RE_READ,
                    paper_key=k, category=RE_READ_SKIP_LLM,
                    detail=f"{type(e).__name__}: {e}",
                    provider=provider, model_tried=model,
                    pipeline="re_read",
                    extra={
                        "selector": selector_name,
                        "source": source_name,
                    },
                )
            print(
                f"  ✗ {k}  unexpected {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    return report


def format_report(report: ReReadReport) -> str:
    """Human-readable summary for CLI stdout."""
    lines = [
        f"re-read batch: selector={report.selector_name}  "
        f"source={report.source_name}",
        "",
    ]
    lines.append(
        f"  requested:  {report.count_requested}"
    )
    lines.append(
        f"  selected:   {report.count_selected}"
    )
    if report.dry_run:
        lines.append("  (dry-run — no LLM calls, no writes)")
        return "\n".join(lines)
    lines.append(
        f"  succeeded:  {len(report.success_keys)}  "
        f"(total {report.total_sections_updated} section(s) updated)"
    )
    lines.append(
        f"  skipped:    {len(report.skip_keys)}"
    )
    if report.skip_keys:
        lines.append("")
        lines.append("  Skips:")
        for k, reason in report.skip_keys:
            lines.append(f"    - {k}: {reason[:100]}")
    return "\n".join(lines)
