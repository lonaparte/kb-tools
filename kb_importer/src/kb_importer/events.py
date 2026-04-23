"""Events log: structured persistent record of kb-importer /
kb-write runtime events worth looking at later.

v26 replaces the v25-vintage `skip_log` module with a more general
event stream. "Skip" is just one `event_type` among several —
future ones may include `re_read` (batch re-summarisation run
outcomes), `pipeline_halt`, and whatever other operational events
deserve aggregation by `kb-mcp report`.

Location: `<kb_root>/.kb-mcp/events.jsonl` — co-located with
audit.log and index.sqlite, auto-included in snapshot, NOT git-
tracked. Append-only JSONL, one event per line.

Format: one JSON object per line. Fields (all optional except
`ts` and `event_type`):

  ts:               RFC 3339 UTC timestamp
  event_type:       enum — "fulltext_skip" | "re_read" | ...
                    (see EVENT_TYPE_* constants below)
  paper_key:        Zotero / paper key this event concerns
  category:         sub-classification inside the event_type.
                    For fulltext_skip: "quota_exhausted",
                    "llm_bad_request", "llm_other", "pdf_missing",
                    "pdf_unreadable", "longform_failure", "other".
                    For re_read: "success", "skip_mtime_conflict",
                    "skip_llm_error", etc.
  detail:           short human message (≤500 chars)
  provider:         LLM provider name (if applicable)
  model_tried:      primary model name attempted
  fallback_tried:   fallback model name if used
  pipeline:         "short" | "long" | "re_read" — which caller
                    triggered the event
  extra:            dict — event-type-specific fields without a
                    dedicated column (e.g. re_read selector name,
                    seed paper for related-to-recent, etc.)

Readers tolerate malformed lines (skip + warn). Writers never
raise — event-logging MUST NOT break the operation that triggered
it. Critical I/O failures get logged via the Python logger at
warning level and swallowed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger(__name__)


# Event types (top-level bucket). Keep small — unknown values are
# coerced to EVENT_OTHER so readers don't break on typos.
#
# Design principle: events.jsonl records DELIBERATE library-level
# actions and their outcomes, not fine-grained writes. For every
# kb-write call (thought create, tag add, ai_zone append, ...)
# there's already a line in audit.log; those stay there. events.jsonl
# is the "I ran a big operation on my library" trail, which `kb-mcp
# report` aggregates into a periodic digest.
EVENT_FULLTEXT_SKIP = "fulltext_skip"   # per-paper fulltext failure (diagnostic)
EVENT_RE_READ       = "re_read"         # per-paper outcome inside a re-read batch
EVENT_RE_SUMMARIZE  = "re_summarize"    # per-paper outcome of single re-summarize
EVENT_IMPORT_RUN    = "import_run"      # summary of one `kb-importer import` run
EVENT_CITATIONS_RUN = "citations_run"   # summary of one kb-citations subcommand
EVENT_INDEX_OP      = "index_op"        # reindex / snapshot-export / snapshot-import
EVENT_OTHER         = "other"

_ALLOWED_EVENT_TYPES = {
    EVENT_FULLTEXT_SKIP,
    EVENT_RE_READ,
    EVENT_RE_SUMMARIZE,
    EVENT_IMPORT_RUN,
    EVENT_CITATIONS_RUN,
    EVENT_INDEX_OP,
    EVENT_OTHER,
}


# ---- fulltext_skip categories (sub-type of EVENT_FULLTEXT_SKIP) ----
# These mirror the old skip_log.REASON_* constants and carry the
# same semantics. Kept as module-level constants so callers can
# import them by name rather than passing string literals.
REASON_QUOTA_EXHAUSTED   = "quota_exhausted"     # 429 / daily RPD / token cap
REASON_LLM_BAD_REQUEST   = "llm_bad_request"     # HTTP 400 / prompt-shape issues
REASON_LLM_OTHER         = "llm_other"           # 500 / timeout / unknown LLM failure
REASON_PDF_MISSING       = "pdf_missing"         # attachment absent on disk
REASON_PDF_UNREADABLE    = "pdf_unreadable"      # pdfplumber/pypdf both failed
REASON_ALREADY_PROCESSED = "already_processed"   # fulltext_processed=true (normal)
REASON_LONGFORM_FAILURE  = "longform_failure"    # chapter split / per-chapter LLM failed
REASON_OTHER             = "other"               # catch-all

_ALLOWED_SKIP_CATEGORIES = {
    REASON_QUOTA_EXHAUSTED, REASON_LLM_BAD_REQUEST, REASON_LLM_OTHER,
    REASON_PDF_MISSING, REASON_PDF_UNREADABLE, REASON_ALREADY_PROCESSED,
    REASON_LONGFORM_FAILURE, REASON_OTHER,
}

# "Normal" skip categories — not surfaced by `kb-mcp report` unless
# the user passes --include-normal. A completed paper being
# "skipped" because it's already processed isn't a problem; the
# agg report is for "things that need attention".
NORMAL_SKIP_CATEGORIES = {REASON_ALREADY_PROCESSED}


# ---- re_read categories (sub-type of EVENT_RE_READ) ----
RE_READ_SUCCESS            = "success"                 # splice completed
RE_READ_SKIP_MTIME         = "skip_mtime_conflict"     # md changed mid-run
RE_READ_SKIP_LLM           = "skip_llm_error"          # LLM pass failed (reuses skip-style detail)
RE_READ_SKIP_PDF           = "skip_pdf_missing"        # no PDF for paper
RE_READ_SKIP_NOT_PROCESSED = "skip_not_processed"      # fulltext_processed != true — re_summarize needs initial fulltext first
RE_READ_DRYRUN             = "dryrun_selected"         # chosen but not executed
# Parity with re_summarize: a batch-selected paper whose md no
# longer exists / whose target can't be resolved is a pre-LLM
# failure, not an LLM failure. Without this category the kb-mcp
# report would overcount LLM errors when the batch source+selector
# combination yields stale keys (e.g. an orphan in the source
# index after a delete, or a key typo passed through the selector
# as a forced target).
RE_READ_SKIP_BAD_TARGET    = "skip_bad_target"

_ALLOWED_RE_READ_CATEGORIES = {
    RE_READ_SUCCESS, RE_READ_SKIP_MTIME, RE_READ_SKIP_LLM,
    RE_READ_SKIP_PDF, RE_READ_SKIP_NOT_PROCESSED, RE_READ_DRYRUN,
    RE_READ_SKIP_BAD_TARGET,
}


# ---- re_summarize categories (sub-type of EVENT_RE_SUMMARIZE) ----
# Written by kb_write.ops.re_summarize when the single-paper
# correction workflow finishes. Distinct from EVENT_RE_READ because
# re-summarize is user/agent-initiated on one paper, while re_read
# is batch-initiated across many via a selector.
RE_SUMMARIZE_SUCCESS       = "success"                 # splice completed
RE_SUMMARIZE_NO_CHANGE     = "no_change"               # LLM judged stored text correct; nothing spliced
RE_SUMMARIZE_SKIP_MTIME    = "skip_mtime_conflict"
RE_SUMMARIZE_SKIP_LLM      = "skip_llm_error"
RE_SUMMARIZE_SKIP_PDF      = "skip_pdf_missing"
RE_SUMMARIZE_SKIP_NOT_PROCESSED = "skip_not_processed"
# v27 addition: user-error cases that failed BEFORE any LLM call.
# Previously these silently fell through to skip_llm_error, which
# made the daily report misleading ("LLM had 8 failures" when
# really the user typed a wrong paper key 8 times).
RE_SUMMARIZE_SKIP_BAD_TARGET    = "skip_bad_target"    # md path doesn't resolve / paper doesn't exist

_ALLOWED_RE_SUMMARIZE_CATEGORIES = {
    RE_SUMMARIZE_SUCCESS, RE_SUMMARIZE_NO_CHANGE,
    RE_SUMMARIZE_SKIP_MTIME, RE_SUMMARIZE_SKIP_LLM,
    RE_SUMMARIZE_SKIP_PDF, RE_SUMMARIZE_SKIP_NOT_PROCESSED,
    RE_SUMMARIZE_SKIP_BAD_TARGET,
}


# ---- import_run categories (sub-type of EVENT_IMPORT_RUN) ----
# One event per `kb-importer import` command invocation, written at
# the tail of the run. Summarises the whole batch — NOT one per paper.
#
# Rationale: an import run can touch 100 papers; writing 100 events
# to summarise "I ran an import" would drown out everything else in
# the digest. The per-paper failures are already recorded as
# EVENT_FULLTEXT_SKIP; the IMPORT_RUN event is the "I did this"
# landmark.
IMPORT_RUN_OK       = "ok"           # command completed, no errors
IMPORT_RUN_PARTIAL  = "partial"      # completed but some papers skipped / LLM failed
IMPORT_RUN_ABORTED  = "aborted"      # halted early (quota exhausted, fatal error)

_ALLOWED_IMPORT_RUN_CATEGORIES = {
    IMPORT_RUN_OK, IMPORT_RUN_PARTIAL, IMPORT_RUN_ABORTED,
}


# ---- citations_run categories (sub-type of EVENT_CITATIONS_RUN) ----
# One event per kb-citations subcommand run: fetch / link /
# refresh-counts. The `extra.subcommand` field disambiguates which.
CITATIONS_RUN_OK      = "ok"
CITATIONS_RUN_PARTIAL = "partial"     # some papers failed (provider 404 / network blips)
CITATIONS_RUN_ABORTED = "aborted"

_ALLOWED_CITATIONS_RUN_CATEGORIES = {
    CITATIONS_RUN_OK, CITATIONS_RUN_PARTIAL, CITATIONS_RUN_ABORTED,
}


# ---- index_op categories (sub-type of EVENT_INDEX_OP) ----
# `kb-mcp reindex` + `kb-mcp snapshot export` + `kb-mcp snapshot
# import`. The `extra.subcommand` field disambiguates.
INDEX_OP_OK      = "ok"
INDEX_OP_FAILED  = "failed"

_ALLOWED_INDEX_OP_CATEGORIES = {
    INDEX_OP_OK, INDEX_OP_FAILED,
}


EVENTS_LOG_RELPATH = ".kb-mcp/events.jsonl"


def record_event(
    kb_root: Path,
    *,
    event_type: str,
    paper_key: str | None = None,
    category: str | None = None,
    detail: str = "",
    provider: str | None = None,
    model_tried: str | None = None,
    fallback_tried: str | None = None,
    pipeline: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append one JSONL event entry. Never raises.

    See module docstring for field semantics. Unknown event_types or
    categories are logged (Python logger warning) and coerced to the
    catch-all buckets so malformed call sites don't silently drop
    events.

    Writers: prefer constants (EVENT_*, REASON_*, RE_READ_*) over
    string literals at call sites.
    """
    if event_type not in _ALLOWED_EVENT_TYPES:
        log.warning(
            "events.record: unknown event_type %r → 'other'", event_type,
        )
        event_type = EVENT_OTHER

    # Category validation is event-type-aware. Unknown categories
    # still get written (users may add new ones in extra-detailed
    # scenarios); we only warn.
    if category is not None:
        if event_type == EVENT_FULLTEXT_SKIP and category not in _ALLOWED_SKIP_CATEGORIES:
            log.warning(
                "events.record: unknown fulltext_skip category %r", category,
            )
        elif event_type == EVENT_RE_READ and category not in _ALLOWED_RE_READ_CATEGORIES:
            log.warning(
                "events.record: unknown re_read category %r", category,
            )
        elif event_type == EVENT_RE_SUMMARIZE and category not in _ALLOWED_RE_SUMMARIZE_CATEGORIES:
            log.warning(
                "events.record: unknown re_summarize category %r", category,
            )
        elif event_type == EVENT_IMPORT_RUN and category not in _ALLOWED_IMPORT_RUN_CATEGORIES:
            log.warning(
                "events.record: unknown import_run category %r", category,
            )
        elif event_type == EVENT_CITATIONS_RUN and category not in _ALLOWED_CITATIONS_RUN_CATEGORIES:
            log.warning(
                "events.record: unknown citations_run category %r", category,
            )
        elif event_type == EVENT_INDEX_OP and category not in _ALLOWED_INDEX_OP_CATEGORIES:
            log.warning(
                "events.record: unknown index_op category %r", category,
            )

    path = kb_root / EVENTS_LOG_RELPATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("events.record: cannot create parent dir: %s", e)
        return

    entry = {
        "ts":             _utc_now_rfc3339(),
        "event_type":     event_type,
        "paper_key":      paper_key,
        "category":       category,
        "detail":         (detail or "")[:500],
        "provider":       provider,
        "model_tried":    model_tried,
        "fallback_tried": fallback_tried,
        "pipeline":       pipeline,
        "extra":          extra or {},
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError as e:
        log.warning("events.record: append failed: %s (entry: %s)", e, entry)


def read_events(
    kb_root: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    event_types: list[str] | None = None,
) -> list[dict]:
    """Read events, optionally filtered by time window and type.

    Args:
        kb_root:      KB root directory.
        since / until: ISO-aware datetimes. Events outside the
                      [since, until] window are skipped. Either
                      bound may be None (open-ended).
        event_types:  if given, restrict to this list of event_types.

    Malformed lines are skipped with a warning. Returns a list in
    file order (which is chronological for correct writers).
    """
    path = kb_root / EVENTS_LOG_RELPATH
    if not path.exists():
        return []

    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError as e:
                    log.warning(
                        "events.read: malformed line %d: %s", lineno, e,
                    )
                    continue
                if event_types and entry.get("event_type") not in event_types:
                    continue
                if since or until:
                    ts_str = entry.get("ts", "")
                    try:
                        # Accept RFC 3339 "Z"; strip and parse.
                        if ts_str.endswith("Z"):
                            ts_str = ts_str[:-1] + "+00:00"
                        ts = datetime.fromisoformat(ts_str)
                    except ValueError:
                        # Malformed timestamp → skip filter, include
                        # the event (better to show than to hide).
                        out.append(entry)
                        continue
                    if since and ts < since:
                        continue
                    if until and ts > until:
                        continue
                out.append(entry)
    except OSError as e:
        log.warning("events.read: failed to read %s: %s", path, e)
        return []
    return out


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
