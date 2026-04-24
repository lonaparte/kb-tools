"""Fulltext pass for `kb-importer import`.

Extracted from import_cmd.py in v0.28.0. The heavy part of the
pipeline: PDF text extraction, LLM summarisation, md writeback,
chapter-split for long-form works.
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..zotero_reader import ZoteroReader
# 0.29.3: _auto_commit_single_paper was moved to import_pipeline in
# the 0.28.0 G-split (kb-importer: split 1505-line import_cmd.py
# into per-phase modules) but this file's two call sites never got
# an import line added. The fulltext path's per-paper git commit
# therefore raised NameError at runtime. Caught by the 0.29.3
# cross-module-import lint.
from .import_pipeline import _auto_commit_single_paper

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Fulltext pass
# ----------------------------------------------------------------------

def _run_fulltext_pass(
    args: argparse.Namespace,
    cfg: Config,
    reader: ZoteroReader,
    candidate_keys: set[str],
) -> int:
    """Extract fulltext + LLM summarise + writeback for each paper.

    Runs after metadata import. Work set = candidate_keys (already
    merged positional keys + --only-key + filters in _resolve_paper_keys)
    filtered by fulltext_processed state. Routing per paper is
    determined by its Zotero item_type via kb_importer.eligibility:

      - short → journal articles etc.: current 7-section pipeline.
      - long  → books / theses: chapter-splitting pipeline (stage 1+2
                of the long-form design; stage 3 global reduce is v23).
      - none  → webpages etc.: skipped, counted as skipped_ineligible.

    --longform / --no-longform override per-paper routing (diagnostic);
    --longform-dryrun runs only chapter detection without calling LLM.

    Does NOT talk to the MCP indexer — caller should run
    `kb-mcp index` after this to pick up new chunks.

    Returns 0 on clean run, 1 if any paper hit an unrecoverable error
    (missing LLM API key, LLM failed, writeback failed).
    """
    from ..fulltext import extract_fulltext, SOURCE_UNAVAILABLE
    from ..summarize import (
        build_provider_from_env, summarize_paper, SummarizerError,
        QuotaExhaustedError, BadRequestError,
    )
    from ..fulltext_writeback import (
        is_fulltext_processed, writeback_summary,
    )
    from ..md_builder import paper_md_path
    from ..eligibility import fulltext_mode

    # Use candidate_keys directly — no re-filter by --only-key here.
    # The metadata phase already merged positional keys + --only-key
    # + filters into candidate_keys; filtering here would drift the
    # two phases' work sets apart (fixed in v22; previously a known
    # source of confusion where positional keys showed up in metadata
    # output but vanished from fulltext output).
    #
    # We only need: "did metadata import succeed?" (md file exists)
    # and "already processed?" (skip unless --force-fulltext).
    work: list[str] = []
    skipped_already_processed = 0
    skipped_missing_md = 0
    for key in sorted(candidate_keys):
        md_path = paper_md_path(cfg.kb_root, key)
        if not md_path.is_file():
            skipped_missing_md += 1
            continue
        if not args.force_fulltext and is_fulltext_processed(md_path):
            skipped_already_processed += 1
            continue
        work.append(key)

    if not work:
        print(
            f"\nFulltext: nothing to do "
            f"(skipped {skipped_already_processed} already processed, "
            f"{skipped_missing_md} missing md; pass --force-fulltext "
            f"to reprocess)."
        )
        return 0

    # LLM provider is shared between short and long pipelines. Skip
    # construction in dryrun (we don't call the LLM).
    provider = None
    if not args.longform_dryrun:
        try:
            provider = build_provider_from_env(
                args.fulltext_provider, args.fulltext_model,
            )
        except SummarizerError as e:
            print(f"\nFulltext: {e}", file=sys.stderr)
            return 1

    # Fallback state for daily-quota exhaustion. Shared between the
    # short and long pipelines so a daily-quota hit during the short
    # pass carries into the long pass (same session, same API key →
    # same quota pool). Structure:
    #   fallback_state["enabled"]  — True if user allowed fallback
    #   fallback_state["model"]    — name to switch to on first hit
    #   fallback_state["activated"]— True after the switch happened
    #   fallback_state["stop"]     — True when even the fallback
    #                                model hit quota; caller bails
    #                                out of remaining work.
    fallback_state: dict = {
        "enabled": (
            args.fulltext_provider == "gemini"
            and not args.no_fulltext_fallback
            and bool((args.fulltext_fallback_model or "").strip())
        ),
        "model": args.fulltext_fallback_model or "",
        "activated": False,
        "stop": False,
        # v0.28.2: track models that have already raised a permanent
        # BadRequestError this run. Once a model is in here, we don't
        # send anything else to it in this batch — retries would just
        # hit the same 400/404. If the primary ends up in here, we
        # try to switch to the fallback model once (same as quota).
        "bad_request_models": set(),
    }

    def _try_fallback_after_quota(
        err: QuotaExhaustedError, key: str,
    ) -> bool:
        """Decide whether to switch provider.model to the fallback
        model after a QuotaExhaustedError. Returns True if the caller
        should retry `key` on the new model; False if the quota hit
        was unrecoverable (either fallback disabled, already activated
        and hit again, or non-gemini provider).

        Side effects:
          - Mutates `provider.model` on activation (session-sticky).
          - Sets fallback_state["stop"] = True when the fallback
            itself hit quota — caller then exits the loop.
          - Rate-limit (per-minute) quotas are NOT a fallback trigger:
            the caller should sleep(retry_after) and retry same model.
        """
        # Per-minute quotas: short sleep + retry, don't switch.
        if err.quota_type == "rate":
            import time
            delay = err.retry_after if err.retry_after else 30.0
            delay = min(delay, 120.0)  # cap at 2 min to avoid hangs
            print(
                f"  … {key}  rate-limit ({err.model}); sleeping "
                f"{delay:.0f}s before retry",
                file=sys.stderr,
            )
            time.sleep(delay)
            return True
        # Daily (or unknown → treat as daily) quotas.
        if not fallback_state["enabled"]:
            print(
                f"  ✗ {key}  daily quota exhausted on {err.model}; "
                f"fallback disabled (--no-fulltext-fallback or empty "
                f"--fulltext-fallback-model). Stopping fulltext pass.",
                file=sys.stderr,
            )
            fallback_state["stop"] = True
            return False
        if fallback_state["activated"]:
            # Already switched once; the fallback itself just hit
            # quota. We deliberately don't chain further — per the
            # original design decision: "if 2.5-pro's daily quota
            # also runs out, stop; don't cascade down further
            # tiers". Stop the batch.
            print(
                f"  ✗ {key}  daily quota exhausted on fallback "
                f"{err.model} too. Stopping fulltext pass. Remaining "
                f"papers will need a separate run (e.g. tomorrow "
                f"after quota reset, or with a different API key).",
                file=sys.stderr,
            )
            fallback_state["stop"] = True
            return False
        # First activation.
        old_model = provider.model if provider else err.model
        try:
            provider.model = fallback_state["model"]
        except Exception:
            # Defensive: if provider is None (shouldn't be — we're in
            # the non-dryrun path) or immutable, bail cleanly.
            fallback_state["stop"] = True
            return False
        fallback_state["activated"] = True
        retry_note = (
            f" (primary retry window: {err.retry_after:.0f}s)"
            if err.retry_after else ""
        )
        print(
            f"  ↓ {key}  daily quota on {old_model}; switching to "
            f"{fallback_state['model']} for remaining papers"
            f"{retry_note}",
            file=sys.stderr,
        )
        return True

    def _try_fallback_after_bad_request(
        err: BadRequestError, key: str,
    ) -> bool:
        """v0.28.2: BadRequestError = 400 / 404 from the provider.
        Permanent for (input, model); retrying the SAME input to the
        SAME model will always hit the same error. Two cases:

          (a) The error is specific to THIS paper (e.g. fulltext
              length overflow). Switching models won't help, we
              should just record the failure and move on.
          (b) The error is "model not found" / misconfigured model
              name. Every paper will hit it. If fallback is
              configured, activate it once (same mechanism as
              quota); otherwise stop the batch.

        Heuristic for (b): the error message contains "model" AND
        ("not found" OR "not supported" OR "invalid"). This matches
        Google's 404 "models/gemini-X is not found" and also their
        400 "Model gemini-X is not supported" shapes.

        Returns True if the caller should retry `key` on a new model;
        False if this paper should be counted as failed and the
        caller should move on. May set fallback_state["stop"] to
        exit the whole batch when model-itself-bad + no fallback.
        """
        # Mark the model as poisoned for the rest of the run.
        bad_model = err.model if hasattr(err, "model") and err.model else \
            (provider.model if provider else "unknown")
        fallback_state["bad_request_models"].add(bad_model)

        msg = str(err).lower()
        looks_model_related = (
            "model" in msg
            and any(s in msg for s in (
                "not found", "not supported", "invalid", "does not exist",
                "model_not_found",
            ))
        )

        if not looks_model_related:
            # Per-paper BadRequest. No retry, no batch-level action;
            # just surface and move on.
            return False

        # Model-level BadRequest: would affect every paper in the
        # batch. Try fallback once, same as quota flow.
        if not fallback_state["enabled"]:
            print(
                f"  ✗ {key}  model {bad_model!r} rejected request "
                f"(HTTP 400/404) and fallback disabled; stopping "
                f"fulltext pass. Error: {err}",
                file=sys.stderr,
            )
            fallback_state["stop"] = True
            return False

        if fallback_state["activated"]:
            # Already switched once; the fallback itself is also
            # rejecting. Don't chain further.
            print(
                f"  ✗ {key}  fallback model {bad_model!r} also "
                f"rejected (HTTP 400/404). Stopping fulltext pass. "
                f"Error: {err}",
                file=sys.stderr,
            )
            fallback_state["stop"] = True
            return False

        old_model = provider.model if provider else bad_model
        try:
            provider.model = fallback_state["model"]
        except Exception:
            fallback_state["stop"] = True
            return False
        fallback_state["activated"] = True
        print(
            f"  ↓ {key}  model {old_model!r} rejected request as "
            f"invalid/unsupported; switching to "
            f"{fallback_state['model']!r} for remaining papers.",
            file=sys.stderr,
        )
        return True

    # Classify each work item up front so the user sees short vs long
    # vs skipped counts before any LLM spend.
    mode_override = getattr(args, "longform_override", None)
    short_keys: list[str] = []
    long_keys: list[str] = []
    skipped_ineligible = 0
    ineligible_breakdown: dict[str, int] = {}
    for key in work:
        md_path = paper_md_path(cfg.kb_root, key)
        item_type = _peek_item_type(md_path)
        if mode_override:
            mode = mode_override
        else:
            mode = fulltext_mode(item_type)
        if mode == "short":
            short_keys.append(key)
        elif mode == "long":
            long_keys.append(key)
        else:
            skipped_ineligible += 1
            label = item_type or "(unknown)"
            ineligible_breakdown[label] = (
                ineligible_breakdown.get(label, 0) + 1
            )

    provider_label = (
        f"{provider.name}/{provider.model}" if provider else "(dryrun)"
    )
    print(
        f"\nFulltext pass: {len(short_keys)} short, "
        f"{len(long_keys)} long, "
        f"{skipped_ineligible} ineligible "
        f"via {provider_label}"
    )
    if skipped_ineligible:
        detail = ", ".join(
            f"{t}={n}" for t, n in sorted(ineligible_breakdown.items())
        )
        print(f"  ineligible breakdown: {detail}")

    # Aggregated counters across both pipelines.
    source_counts: dict[str, int] = {}
    llm_ok = 0
    llm_fail = 0
    extract_miss = 0
    skipped_longform_existing = 0  # v24: idempotency skip count
    total_prompt_tokens = 0
    total_completion_tokens = 0

    storage_dir = cfg.zotero_storage_dir if cfg.zotero_storage_dir else None

    # ---- Short pipeline ----
    # Defensive dedup. The upstream flow (set[str] → sorted → work →
    # short_keys) cannot produce duplicates today, but a pre-v19
    # version of the import flow enumerated at attachment level and
    # double-summarised papers with multiple PDFs. If a future
    # refactor reintroduces that shape, detect it here, loudly warn
    # the operator, dedup, and continue — raising instead would
    # abort the whole fulltext pass after short work may have
    # already completed, and burning a traceback is less useful
    # than a visible warning plus a correct run.
    if len(set(short_keys)) != len(short_keys):
        seen_s: dict[str, int] = {}
        for k in short_keys:
            seen_s[k] = seen_s.get(k, 0) + 1
        dupes_s = {k: n for k, n in seen_s.items() if n > 1}
        print(
            f"\n⚠  short_keys contained {len(dupes_s)} duplicate paper_key(s) "
            f"(total {sum(n - 1 for n in dupes_s.values())} extra entries): "
            f"{dupes_s}. Deduping and continuing — but this indicates an "
            f"upstream regression: paper-key assembly should be set-based. "
            f"Please report.",
            file=sys.stderr,
        )
        short_keys = list(dict.fromkeys(short_keys))
    for key in short_keys:
        md_path = paper_md_path(cfg.kb_root, key)
        try:
            paper = reader.get_paper(key)
        except Exception as e:
            print(f"  ✗ {key}  could not re-fetch item: {e}",
                  file=sys.stderr)
            extract_miss += 1
            continue

        result = extract_fulltext(
            paper_key=key,
            attachments=paper.attachments,
            reader=reader,
            storage_dir=storage_dir,
        )
        if not result.ok:
            print(f"  – {key}  extract miss ({result.error})")
            extract_miss += 1
            source_counts[SOURCE_UNAVAILABLE] = (
                source_counts.get(SOURCE_UNAVAILABLE, 0) + 1
            )
            # v26: record to skip log for periodic aggregation.
            # We categorise "no PDF at all" vs "PDF present but
            # unreadable" heuristically from result.error — if the
            # error string mentions pdfplumber/pypdf the attachment
            # was found but extraction failed.
            from ..events import (
                record_event, EVENT_FULLTEXT_SKIP,
                REASON_PDF_MISSING, REASON_PDF_UNREADABLE,
            )
            err_lower = (result.error or "").lower()
            if "pdfplumber" in err_lower or "pypdf" in err_lower:
                cat = REASON_PDF_UNREADABLE
            else:
                cat = REASON_PDF_MISSING
            record_event(
                cfg.kb_root,
                event_type=EVENT_FULLTEXT_SKIP,
                paper_key=key, category=cat,
                detail=result.error or "extract miss",
                pipeline="short",
            )
            continue

        if args.longform_dryrun:
            # For short papers, dryrun just reports what would happen.
            print(f"  (dryrun) {key}  [short] would summarise "
                  f"{len(result.text)} chars from {result.source}")
            continue

        authors_s = ", ".join(paper.authors or []) or ""
        # Quota-aware retry loop: if the current model hits quota, we
        # may (a) switch to fallback (daily) or (b) sleep and retry
        # (rate). At most 2 attempts — one primary, one on fallback.
        # A retry is only issued if _try_fallback_after_quota returned
        # True; on False we've already printed why and we abort the
        # paper (llm_fail++) and, if fallback_state["stop"] is set,
        # break out of the short-pipeline loop entirely.
        summary = None
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                summary = summarize_paper(
                    provider=provider,
                    fulltext=result.text,
                    title=paper.title or "",
                    authors=authors_s,
                    year=paper.year or "",
                    doi=paper.doi or "",
                    abstract=paper.abstract or "",
                    max_output_tokens=args.fulltext_max_tokens,
                )
                break  # success
            except QuotaExhaustedError as e:
                if _try_fallback_after_quota(e, key):
                    last_err = e
                    continue  # retry on new model / after sleep
                last_err = e
                break  # caller decided not to retry
            except BadRequestError as e:
                # v0.28.2: HTTP 400/404 → permanent for (input, model).
                # Retry only if the model itself is the problem AND a
                # fallback model is configured (see
                # _try_fallback_after_bad_request heuristic).
                if _try_fallback_after_bad_request(e, key):
                    last_err = e
                    continue
                last_err = e
                break
            except SummarizerError as e:
                last_err = e
                break
            except Exception as e:
                log.exception("unexpected summariser error on %s", key)
                last_err = e
                break

        if summary is None:
            # v26: classify the error and write a structured event so
            # periodic aggregation (`kb-mcp report`) can say
            # "N quota, M bad-request, K unexpected" at a glance.
            # The stderr prints above stay for real-time feedback;
            # events.jsonl persistently records what was lost.
            from ..events import (
                record_event, EVENT_FULLTEXT_SKIP,
                REASON_QUOTA_EXHAUSTED, REASON_LLM_BAD_REQUEST,
                REASON_LLM_OTHER, REASON_OTHER,
            )
            _err_text = str(last_err) if last_err else ""
            # Gather provider/model metadata from fallback_state for
            # the log (we don't have a clean provider handle here
            # because the retry loop reassigns `provider`). Best-effort.
            _provider_name = fallback_state.get("provider")
            _model_tried = fallback_state.get("primary_model")
            _fallback_tried = fallback_state.get("fallback_model") if \
                fallback_state.get("stop") else None

            if isinstance(last_err, QuotaExhaustedError):
                # Message already printed by _try_fallback_after_quota.
                llm_fail += 1
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=REASON_QUOTA_EXHAUSTED,
                    detail=_err_text,
                    provider=_provider_name, model_tried=_model_tried,
                    fallback_tried=_fallback_tried, pipeline="short",
                )
            elif isinstance(last_err, SummarizerError):
                print(f"  ✗ {key}  LLM failed: {last_err}",
                      file=sys.stderr)
                llm_fail += 1
                # v0.28.2: classify by EXCEPTION TYPE, not string
                # matching. BadRequestError is the typed signal that
                # provider returned 400/404 (reviewer's point: the
                # class already existed but wasn't used here; we
                # were string-matching "400" at log time).
                if isinstance(last_err, BadRequestError):
                    cat = REASON_LLM_BAD_REQUEST
                else:
                    cat = REASON_LLM_OTHER
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=cat, detail=_err_text,
                    provider=_provider_name, model_tried=_model_tried,
                    pipeline="short",
                )
            else:
                print(
                    f"  ✗ {key}  unexpected: "
                    f"{type(last_err).__name__}: {last_err}",
                    file=sys.stderr,
                )
                llm_fail += 1
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=REASON_OTHER,
                    detail=f"{type(last_err).__name__}: {_err_text}",
                    provider=_provider_name, model_tried=_model_tried,
                    pipeline="short",
                )
            if fallback_state["stop"]:
                # Exhausted both primary and fallback; stop the whole
                # short pipeline so we don't keep hot-looping 429s.
                print(
                    "\nFulltext short pipeline: halting early due to "
                    "exhausted quota. "
                    f"Completed {llm_ok}, failed {llm_fail}.",
                    file=sys.stderr,
                )
                break
            continue

        try:
            writeback_summary(
                md_path,
                summary_markdown=summary.to_markdown(),
                source=result.source,
                model_label=f"{summary.provider}/{summary.model}",
            )
        except Exception as e:
            log.exception("writeback failed for %s", key)
            print(f"  ✗ {key}  writeback: {type(e).__name__}: {e}",
                  file=sys.stderr)
            llm_fail += 1
            continue

        llm_ok += 1
        source_counts[result.source] = source_counts.get(result.source, 0) + 1
        total_prompt_tokens += summary.prompt_tokens
        total_completion_tokens += summary.completion_tokens
        print(f"  ✓ {key}  [short:{result.source}]  "
              f"in={summary.prompt_tokens} out={summary.completion_tokens}")

        # Per-paper auto-commit. Each successful fulltext writeback
        # gets its own commit — meaningful atomic unit (the md file
        # is self-contained, commit message records the model used),
        # and a mid-run crash leaves completed papers committed while
        # the rest stays re-runnable. No-op when --no-git-commit or
        # not a git repo. Commit failures warn but don't abort the
        # remaining loop.
        _auto_commit_single_paper(
            cfg, args, key, op="fulltext",
            message_body=(
                f"source: {result.source}\n"
                f"model: {summary.provider}/{summary.model}\n"
                f"tokens: in={summary.prompt_tokens} "
                f"out={summary.completion_tokens}"
            ),
        )

    # ---- Long pipeline ----
    if long_keys:
        # Same defensive dedup as short pipeline. See the note there
        # for why this is warn-and-dedup rather than raise — aborting
        # after short pipeline has already spent LLM budget is worse
        # than running long pipeline correctly with a prominent warning.
        if len(set(long_keys)) != len(long_keys):
            seen: dict[str, int] = {}
            for k in long_keys:
                seen[k] = seen.get(k, 0) + 1
            dupes = {k: n for k, n in seen.items() if n > 1}
            print(
                f"\n⚠  long_keys contained {len(dupes)} duplicate paper_key(s) "
                f"(total {sum(n - 1 for n in dupes.values())} extra entries): "
                f"{dupes}. Deduping and continuing — but this indicates an "
                f"upstream regression: paper-key assembly should be set-based. "
                f"Please report.",
                file=sys.stderr,
            )
            long_keys = list(dict.fromkeys(long_keys))
        from ..longform import (
            longform_ingest_paper, LongformError,
        )
        for key in long_keys:
            md_path = paper_md_path(cfg.kb_root, key)
            try:
                paper = reader.get_paper(key)
            except Exception as e:
                print(f"  ✗ {key}  could not re-fetch item: {e}",
                      file=sys.stderr)
                extract_miss += 1
                continue

            result = extract_fulltext(
                paper_key=key,
                attachments=paper.attachments,
                reader=reader,
                storage_dir=storage_dir,
                # Long pipeline needs the ENTIRE book text so
                # split_into_chapters can see all chapter markers.
                # Default truncate=True would drop the middle 30%
                # of any >200K-char book, making chapters 4-12 of
                # a 15-chapter book disappear silently.
                truncate=False,
            )
            if not result.ok:
                print(f"  – {key}  extract miss ({result.error})")
                extract_miss += 1
                source_counts[SOURCE_UNAVAILABLE] = (
                    source_counts.get(SOURCE_UNAVAILABLE, 0) + 1
                )
                from ..events import (
                    record_event, EVENT_FULLTEXT_SKIP,
                    REASON_PDF_MISSING, REASON_PDF_UNREADABLE,
                )
                err_lower = (result.error or "").lower()
                cat = REASON_PDF_UNREADABLE if (
                    "pdfplumber" in err_lower or "pypdf" in err_lower
                ) else REASON_PDF_MISSING
                record_event(
                    cfg.kb_root,
                    event_type=EVENT_FULLTEXT_SKIP,
                    paper_key=key, category=cat,
                    detail=result.error or "extract miss",
                    pipeline="long",
                )
                continue

            # Quota-aware retry loop, same shape as short pipeline.
            # longform_ingest_paper internally calls provider.complete
            # per chapter; QuotaExhaustedError from any chapter bubbles
            # up here. _try_fallback_after_quota mutates provider.model
            # in place, so the retry runs on the new (fallback) model.
            outcome = None
            last_err: Exception | None = None
            for _attempt in range(2):
                try:
                    outcome = longform_ingest_paper(
                        cfg=cfg,
                        paper_key=key,
                        paper=paper,
                        fulltext=result.text,
                        pdf_path=result.pdf_path,
                        provider=provider,
                        max_output_tokens=args.fulltext_max_tokens,
                        dryrun=args.longform_dryrun,
                        # --force-fulltext overrides the idempotency
                        # skip. Without --force, a paper whose
                        # chapter thoughts already exist on disk is
                        # skipped (no LLM spend).
                        force_regenerate=args.force_fulltext,
                    )
                    break
                except QuotaExhaustedError as e:
                    if _try_fallback_after_quota(e, key):
                        last_err = e
                        continue
                    last_err = e
                    break
                except BadRequestError as e:
                    # v0.28.2: same logic as short pipeline.
                    if _try_fallback_after_bad_request(e, key):
                        last_err = e
                        continue
                    last_err = e
                    break
                except LongformError as e:
                    last_err = e
                    break
                except Exception as e:
                    log.exception("unexpected longform error on %s", key)
                    last_err = e
                    break

            if outcome is None:
                from ..events import (
                    record_event, EVENT_FULLTEXT_SKIP,
                    REASON_QUOTA_EXHAUSTED, REASON_LONGFORM_FAILURE,
                    REASON_OTHER,
                )
                _err_text = str(last_err) if last_err else ""
                _provider_name = fallback_state.get("provider")
                _model_tried = fallback_state.get("primary_model")
                _fallback_tried = fallback_state.get("fallback_model") if \
                    fallback_state.get("stop") else None

                if isinstance(last_err, QuotaExhaustedError):
                    llm_fail += 1
                    record_event(
                        cfg.kb_root,
                        event_type=EVENT_FULLTEXT_SKIP,
                        paper_key=key, category=REASON_QUOTA_EXHAUSTED,
                        detail=_err_text,
                        provider=_provider_name, model_tried=_model_tried,
                        fallback_tried=_fallback_tried, pipeline="long",
                    )
                elif isinstance(last_err, LongformError):
                    print(f"  ✗ {key}  longform failed: {last_err}",
                          file=sys.stderr)
                    llm_fail += 1
                    record_event(
                        cfg.kb_root,
                        event_type=EVENT_FULLTEXT_SKIP,
                        paper_key=key, category=REASON_LONGFORM_FAILURE,
                        detail=_err_text,
                        provider=_provider_name, model_tried=_model_tried,
                        pipeline="long",
                    )
                else:
                    print(
                        f"  ✗ {key}  unexpected: "
                        f"{type(last_err).__name__}: {last_err}",
                        file=sys.stderr,
                    )
                    llm_fail += 1
                    record_event(
                        cfg.kb_root,
                        event_type=EVENT_FULLTEXT_SKIP,
                        paper_key=key, category=REASON_OTHER,
                        detail=f"{type(last_err).__name__}: {_err_text}",
                        provider=_provider_name, model_tried=_model_tried,
                        pipeline="long",
                    )
                if fallback_state["stop"]:
                    print(
                        "\nFulltext long pipeline: halting early due "
                        "to exhausted quota. "
                        f"Completed {llm_ok}, failed {llm_fail}.",
                        file=sys.stderr,
                    )
                    break
                continue

            if args.longform_dryrun:
                print(
                    f"  (dryrun) {key}  [long] "
                    f"{len(outcome.chapters)} chapters via "
                    f"{outcome.split_source}"
                )
                for ch in outcome.chapters[:10]:
                    title = (ch.title or "(untitled)")[:60]
                    print(f"      ch{ch.number:02d}: {title}")
                if len(outcome.chapters) > 10:
                    print(f"      ... +{len(outcome.chapters) - 10} more")
                continue

            # Idempotency skip: existing chapter thoughts on disk,
            # --force-fulltext not set. longform_ingest_paper returns
            # an empty outcome with split_source="skipped_idempotent"
            # in this case (no LLM spend, no file writes). Don't
            # count as success OR failure — it's "already done".
            if outcome.split_source == "skipped_idempotent":
                skipped_longform_existing += 1
                print(
                    f"  — {key}  [long] already has chapter "
                    f"thoughts on disk; skipping "
                    f"(pass --force-fulltext to regenerate)"
                )
                continue

            llm_ok += 1
            source_counts[result.source] = (
                source_counts.get(result.source, 0) + 1
            )
            total_prompt_tokens += outcome.prompt_tokens
            total_completion_tokens += outcome.completion_tokens
            print(
                f"  ✓ {key}  [long:{outcome.split_source}] "
                f"{outcome.chapters_written} chapters, "
                f"in={outcome.prompt_tokens} out={outcome.completion_tokens}"
            )

            # Per-book auto-commit: one commit encompassing the parent
            # paper md (with its chapter-index writeback) AND every
            # chapter thought file produced in this run. This keeps a
            # book's ingest as one atomic unit in git history —
            # reverting a single commit undoes the entire longform
            # ingest cleanly. Per-chapter commits would make revert
            # painful and bloat `git log` for 15-50 chapter books.
            chapter_paths = [
                co.thought_path for co in outcome.per_chapter
                if getattr(co, "thought_path", None) is not None
            ]
            _auto_commit_single_paper(
                cfg, args, key, op="longform",
                extra_files=chapter_paths,
                message_body=(
                    f"split: {outcome.split_source}\n"
                    f"chapters: {outcome.chapters_written}\n"
                    f"model: {provider.name}/{provider.model}\n"
                    f"tokens: in={outcome.prompt_tokens} "
                    f"out={outcome.completion_tokens}"
                ),
            )

    # ---- Final report ----
    print(f"\nFulltext done: "
          f"{llm_ok} summarised, "
          f"{extract_miss} extract-miss, "
          f"{llm_fail} llm-fail, "
          f"{skipped_ineligible} ineligible")
    if skipped_longform_existing:
        print(f"  (longform idempotency: skipped "
              f"{skipped_longform_existing} book(s) already ingested; "
              f"pass --force-fulltext to regenerate)")
    if source_counts:
        print("  sources: " + ", ".join(
            f"{k}={v}" for k, v in sorted(source_counts.items())
        ))
    print(f"  tokens: prompt={total_prompt_tokens}, "
          f"completion={total_completion_tokens}")
    print("  next: run `kb-mcp index` to pick up the new chunks.")
    return 0 if llm_fail == 0 else 1


def _peek_item_type(md_path: Path) -> str:
    """Read just the item_type field from an md's frontmatter.

    Thin wrapper around md_io.peek_frontmatter. Empty string means
    "unknown" — fulltext_mode() treats it as the conservative
    "short" default.
    """
    from ..md_io import peek_frontmatter
    meta = peek_frontmatter(md_path)
    if meta is None:
        return ""
    v = meta.get("item_type")
    return str(v) if v else ""
