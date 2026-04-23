"""`kb-write re-summarize <paper_key>`: AI-driven correction of an
existing `## AI Summary (from Full Text)` region in a paper md.

Motivation: the first-pass fulltext summary (produced by kb-importer
--fulltext) is authoritative but not infallible — the LLM may miss
something in Figure 7, mis-state a worst-case condition, or conflate
two sections. Users re-reading a paper sometimes spot these errors.
v26 gives them (and the agent) a way to push the correction back
into the summary, not by hand-editing but by running a second,
independent LLM pass and diffing the two.

Flow (per paper, ONE paper at a time — this is not a batch command):

  1. Locate the paper md (accepts `KEY`, `papers/KEY`, or
     `papers/KEY.md`). Must be kind=paper and must already have
     fulltext_processed=true — re-summarise is correction, not
     initial summarisation.
  2. Open the PDF via kb-importer's extractor (soft-dep: kb_importer).
  3. Ask the LLM to produce a NEW 7-section summary.
  4. Ask the LLM to diff new-vs-old per section and, for each
     disagreement, locate the evidence in the PDF and judge which
     version is correct.
  5. For sections marked verdict='new' (and only those), splice
     the new section content into the md's summary region.
     Sections with verdict='old' or 'tied' are left untouched —
     re-summarise NEVER rewrites what the first pass got right.
  6. Write via atomic_write + mtime guard + git auto-commit.

Output (stdout, human-readable):
  - per-section verdict table: `§3 | NEW (fig.7 evidence)`
                               `§5 | OLD kept`
  - summary line: "Updated 2 of 7 sections; 3 unchanged; 2 tied."
  - git commit sha (if enabled).

Strict constraints:
  - The 7-section structure is preserved bit-for-bit: same headings,
    same kb-fulltext marker positions. Only section BODIES change.
  - Book chapters (papers/<KEY>-chNN.md) are supported — re-summarise
    runs on a single md, doesn't know or care whether it's a whole
    paper or a chapter.
  - If kb_importer isn't installed or the PDF is missing, the
    command fails cleanly with a clear error (no partial writes).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..atomic import atomic_write, assert_mtime_unchanged, write_lock
from ..config import WriteContext
from ..git import auto_commit
from ..paths import NodeAddress, parse_target
from ..reindex import trigger_reindex
from ..rules import RuleViolation


log = logging.getLogger(__name__)


# Fulltext region markers — must match md_io's constants in kb_importer.
# We re-declare them here so kb_write doesn't hard-depend on kb_importer
# being installed for any path other than re-summarise itself.
FULLTEXT_START = "<!-- kb-fulltext-start -->"
FULLTEXT_END = "<!-- kb-fulltext-end -->"

# The 7 sections we expect inside the fulltext region. Identified by
# the canonical English+Chinese heading pattern kb-importer emits.
# Section N is located by scanning for a line of the form
# `## <N>. <anything>` inside the fulltext region.
SECTION_COUNT = 7


@dataclass
class SectionVerdict:
    """Per-section judgment returned by the LLM."""
    section: int                    # 1..7
    verdict: str                    # "new" | "old" | "tied"
    reason: str = ""                # short LLM rationale (displayed)
    new_content: str = ""           # only populated when verdict="new"


@dataclass
class ReSummarizeReport:
    paper_key: str
    md_path: Path
    mtime_after: float
    verdicts: list[SectionVerdict] = field(default_factory=list)
    git_sha: str | None = None
    reindexed: bool = False

    def summary_line(self) -> str:
        n_new = sum(1 for v in self.verdicts if v.verdict == "new")
        n_old = sum(1 for v in self.verdicts if v.verdict == "old")
        n_tied = sum(1 for v in self.verdicts if v.verdict == "tied")
        return (
            f"Updated {n_new} of {len(self.verdicts)} sections; "
            f"{n_old} unchanged; {n_tied} tied."
        )


class ReSummarizeError(Exception):
    """Raised for any structured failure of re-summarise (missing
    PDF, malformed md, LLM contract violation, etc.). Callers get a
    readable message and no partial writes happened.

    v27: carries `code` so the re-read classifier can route on a
    stable tag instead of substring-matching the message. Codes:

      "not_processed"  — frontmatter fulltext_processed != true
      "pdf_missing"    — no PDF found for this paper
      "mtime_conflict" — md changed between read and write
      "bad_request"    — LLM returned malformed / non-JSON output
      "llm_other"      — everything else LLM-side

    Code defaults to "llm_other" unless the raise site passes
    `code=`. Existing message text is preserved for display.
    """

    def __init__(self, message: str, *, code: str = "llm_other"):
        super().__init__(message)
        self.code = code


def re_summarize(
    ctx: WriteContext,
    paper_target: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ReSummarizeReport:
    """Entry point. See module docstring for semantics.

    `paper_target` accepts:
      - "ABCD1234"                 (bare key → resolved to papers/ABCD1234.md)
      - "papers/ABCD1234"
      - "papers/ABCD1234.md"
      - "papers/BOOKKEY-ch03"      (book chapter — fully supported)

    `provider` / `model` override kb-importer's configured fulltext
    provider for this run; pass None to use the configured default.

    Side effect (v26.x): every terminal outcome — success, no-change,
    or any ReSummarizeError — is recorded as a single event_type=
    re_summarize entry in <kb_root>/.kb-mcp/events.jsonl. This makes
    `kb-mcp report` aggregate re-summarize activity alongside
    re-read and fulltext-skip. Event logging is best-effort and
    never swallows the caller's exception — if the write fails, the
    original outcome is preserved.
    """
    try:
        report = _re_summarize_core(
            ctx, paper_target, provider=provider, model=model,
        )
    except ReSummarizeError as e:
        _record_re_summarize_failure(ctx.kb_root, paper_target, e,
                                     provider=provider, model_tried=model)
        raise
    except Exception as e:
        # Unexpected crash — still try to log it so the run shows up
        # in the periodic report instead of disappearing.
        _record_re_summarize_failure(ctx.kb_root, paper_target, e,
                                     provider=provider, model_tried=model)
        raise

    _record_re_summarize_success(
        ctx.kb_root, report, provider=provider, model_tried=model,
    )
    return report


def _re_summarize_core(
    ctx: WriteContext,
    paper_target: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ReSummarizeReport:
    """Body of re_summarize; wrapped by re_summarize() so the wrapper
    can emit events around it without complicating the happy path."""
    address, md_path = _resolve_paper_md(ctx.kb_root, paper_target)
    original_text = md_path.read_text(encoding="utf-8")

    # Preflight: must be a processed paper.
    _require_processed_paper(original_text, paper_target)

    # Read old sections out of the fulltext region.
    old_region = _extract_fulltext_region(original_text)
    if old_region is None:
        raise ReSummarizeError(
            f"{paper_target}: cannot locate <!-- kb-fulltext-start/end --> "
            f"markers in md body. Run `kb-write doctor` or "
            f"`kb-importer import papers --fulltext` to regenerate."
        )
    old_sections = _split_into_sections(old_region)
    if len(old_sections) != SECTION_COUNT:
        raise ReSummarizeError(
            f"{paper_target}: existing summary has "
            f"{len(old_sections)} section(s), expected {SECTION_COUNT}. "
            f"Structure is non-standard — cannot re-summarise automatically."
        )

    # Fetch PDF + run new LLM summary (delegated to kb_importer's
    # fulltext pipeline via a thin adapter).
    new_sections = _run_new_summary_pass(
        ctx.kb_root, address.key, original_text,
        provider=provider, model=model,
    )
    if len(new_sections) != SECTION_COUNT:
        raise ReSummarizeError(
            f"{paper_target}: new LLM run produced "
            f"{len(new_sections)} section(s), expected {SECTION_COUNT}. "
            f"Not safe to splice — aborting without changes."
        )

    # Per-section diff + LLM judge.
    verdicts = _judge_sections(
        ctx.kb_root, address.key, old_sections, new_sections,
        provider=provider, model=model,
    )

    # Splice: build the new fulltext region, replacing only the
    # sections the LLM marked verdict="new". Everything else stays
    # exactly as it was, to preserve wording the first pass got right.
    merged_sections = [
        v.new_content if v.verdict == "new" else old_sections[i]
        for i, v in enumerate(verdicts)
    ]
    new_region = _rejoin_sections(merged_sections)
    new_text = _replace_fulltext_region(original_text, new_region)

    if new_text == original_text:
        # All verdicts were "old" or "tied" — the LLM agreed with
        # every existing section. No change, no commit, but still
        # verify mtime to catch concurrent edits.
        assert_mtime_unchanged(md_path, md_path.stat().st_mtime)
        return ReSummarizeReport(
            paper_key=address.key, md_path=md_path,
            mtime_after=md_path.stat().st_mtime,
            verdicts=verdicts, git_sha=None, reindexed=False,
        )

    # (We don't prepend a changelog line to the md. The git commit
    # message is the canonical record; an in-md breadcrumb would
    # fight the splice — it'd drift the ai-zone mtime and collide
    # with ai_zone.append. If we want per-run breadcrumbs later,
    # the audit log under .kb-mcp/ is the right home.)

    if ctx.dry_run:
        return ReSummarizeReport(
            paper_key=address.key, md_path=md_path,
            mtime_after=0.0, verdicts=verdicts,
        )

    mtime_before = md_path.stat().st_mtime
    with write_lock(ctx.kb_root) if ctx.lock else _nullcontext():
        atomic_write(md_path, new_text, expected_mtime=mtime_before)
        mtime_after = md_path.stat().st_mtime

        git_sha = None
        if ctx.git_commit:
            n_new = sum(1 for v in verdicts if v.verdict == "new")
            git_sha = auto_commit(
                ctx.kb_root, [md_path],
                op="re_summarize",
                target=address.md_rel_path,
                message_body=(
                    f"re-summarize {address.key}: updated {n_new} of "
                    f"{SECTION_COUNT} sections"
                ),
                enabled=True,
            )
        reindexed = trigger_reindex(ctx.kb_root, enabled=ctx.reindex)

    return ReSummarizeReport(
        paper_key=address.key, md_path=md_path,
        mtime_after=mtime_after, verdicts=verdicts,
        git_sha=git_sha, reindexed=reindexed,
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

class _nullcontext:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _record_re_summarize_success(
    kb_root: Path,
    report: ReSummarizeReport,
    *,
    provider: str | None,
    model_tried: str | None,
) -> None:
    """Emit a single re_summarize event for a successful run.

    Distinguishes two outcomes:
      - RE_SUMMARIZE_NO_CHANGE: the LLM pass agreed with every
        stored section; no bytes changed.
      - RE_SUMMARIZE_SUCCESS: one or more sections were spliced.

    Never raises — if kb_importer isn't installed or the events
    file can't be written, we swallow silently; events logging
    must not break the caller's successful path.
    """
    try:
        from kb_importer.events import (
            record_event, EVENT_RE_SUMMARIZE,
            RE_SUMMARIZE_SUCCESS, RE_SUMMARIZE_NO_CHANGE,
        )
    except ImportError:
        return
    try:
        n_new = sum(1 for v in report.verdicts if v.verdict == "new")
        if n_new == 0:
            category = RE_SUMMARIZE_NO_CHANGE
            detail = "all sections judged correct; no splice"
        else:
            category = RE_SUMMARIZE_SUCCESS
            detail = (
                f"{n_new} of {len(report.verdicts)} section(s) updated"
            )
        record_event(
            kb_root,
            event_type=EVENT_RE_SUMMARIZE,
            paper_key=report.paper_key,
            category=category,
            detail=detail,
            provider=provider, model_tried=model_tried,
            pipeline="re_summarize",
            extra={"sections_updated": n_new},
        )
    except Exception:
        # Best-effort logging only — don't break the caller.
        pass


def _record_re_summarize_failure(
    kb_root: Path,
    paper_target: str,
    err: Exception,
    *,
    provider: str | None,
    model_tried: str | None,
) -> None:
    """Emit a single re_summarize event for a failed run.

    v27: prefers exception.code for classification; falls back to
    message-substring matching for exceptions that don't carry a
    code. Never raises.
    """
    try:
        from kb_importer.events import (
            record_event, EVENT_RE_SUMMARIZE,
            RE_SUMMARIZE_SKIP_NOT_PROCESSED, RE_SUMMARIZE_SKIP_PDF,
            RE_SUMMARIZE_SKIP_MTIME, RE_SUMMARIZE_SKIP_LLM,
            RE_SUMMARIZE_SKIP_BAD_TARGET,
        )
    except ImportError:
        return
    try:
        msg = str(err)
        code = getattr(err, "code", None)
        cat = None
        if code == "not_processed":
            cat = RE_SUMMARIZE_SKIP_NOT_PROCESSED
        elif code in ("pdf_missing", "no_attachment_keys"):
            # v27: no_attachment_keys is the frontmatter-has-no-
            # zotero_attachment_keys case — it's a variety of
            # "we can't locate the PDF", so it belongs in the
            # PDF bucket, not the LLM bucket.
            cat = RE_SUMMARIZE_SKIP_PDF
        elif code == "mtime_conflict":
            cat = RE_SUMMARIZE_SKIP_MTIME
        elif code in ("bad_request", "llm_other", "quota"):
            cat = RE_SUMMARIZE_SKIP_LLM
        elif code in ("bad_target", "md_not_found", "paper_not_found"):
            cat = RE_SUMMARIZE_SKIP_BAD_TARGET
        if cat is None:
            # No code: fall back to substring-based classification.
            # v27: added an md-not-found branch BEFORE the LLM
            # fallback so mistyped paper keys (md doesn't exist)
            # stop silently landing in skip_llm_error. Also
            # broadened the PDF-locate branch so
            # "no zotero_attachment_keys in frontmatter — cannot
            # locate the PDF" (observed in v26.5 field report)
            # routes to skip_pdf_missing.
            low = msg.lower()
            if (
                "paper md not found" in low
                or "paper not found" in low
                or ("md" in low and "not found" in low)
            ):
                cat = RE_SUMMARIZE_SKIP_BAD_TARGET
            elif "fulltext_processed is not true" in low or "not processed" in low:
                cat = RE_SUMMARIZE_SKIP_NOT_PROCESSED
            elif (
                # PDF-locate failures: any phrasing that implicates
                # the attachment / PDF lookup path. v26 wording
                # covered "pdf missing / not found / no pdf";
                # v27 also covers "no zotero_attachment_keys",
                # "cannot locate the pdf", and "attachment".
                ("pdf" in low and (
                    "missing" in low or "not found" in low
                    or "no pdf" in low or "locate" in low
                ))
                or "no zotero_attachment_keys" in low
                or "cannot locate" in low
            ):
                cat = RE_SUMMARIZE_SKIP_PDF
            elif "mtime" in low or "conflict" in low:
                cat = RE_SUMMARIZE_SKIP_MTIME
            else:
                cat = RE_SUMMARIZE_SKIP_LLM

        # Best-effort paper_key extraction from the target. Parser
        # ambiguity isn't worth dealing with here; if it fails we
        # still record the event with paper_key=None.
        paper_key: str | None = None
        try:
            t = paper_target.strip()
            if "/" in t:
                t = t.split("/", 1)[1]
            if t.endswith(".md"):
                t = t[:-3]
            paper_key = t or None
        except Exception:
            paper_key = None

        record_event(
            kb_root,
            event_type=EVENT_RE_SUMMARIZE,
            paper_key=paper_key,
            category=cat,
            detail=msg[:500],
            provider=provider, model_tried=model_tried,
            pipeline="re_summarize",
        )
    except Exception:
        pass


def _resolve_paper_md(
    kb_root: Path, target: str,
) -> tuple[NodeAddress, Path]:
    """Accept bare key / papers-prefix / full path; return
    (NodeAddress, absolute md path). Confirms the md exists."""
    t = target.strip()
    if "/" not in t:
        t = f"papers/{t}"
    address = parse_target(t)
    if address.node_type != "paper":
        raise RuleViolation(
            f"re-summarize only works on papers; got {address.node_type!r}."
        )
    md = address.md_abspath(kb_root)
    if not md.exists():
        raise ReSummarizeError(
            f"paper md not found: {address.md_rel_path}. "
            f"Has it been imported with `kb-importer import papers`?",
            code="md_not_found",
        )
    return address, md


def _require_processed_paper(md_text: str, target: str) -> None:
    """Confirm frontmatter says fulltext_processed=true. Re-summarise
    is correction, not first-pass summarisation."""
    if not md_text.startswith("---\n"):
        raise ReSummarizeError(
            f"{target}: md has no frontmatter block — cannot re-summarise.",
            code="bad_target",
        )
    end = md_text.find("\n---\n", 4)
    if end < 0:
        raise ReSummarizeError(
            f"{target}: frontmatter block is not terminated.",
            code="bad_target",
        )
    header = md_text[4:end]
    processed = False
    for line in header.splitlines():
        s = line.strip()
        if s.startswith("fulltext_processed:"):
            v = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
            if v in ("true", "yes", "on", "1"):
                processed = True
            break
    if not processed:
        raise ReSummarizeError(
            f"{target}: fulltext_processed is not true. Run "
            f"`kb-importer import papers --fulltext --only-key "
            f"{target}` first to generate an initial summary; "
            f"re-summarize only CORRECTS existing summaries.",
            code="not_processed",
        )


def _extract_fulltext_region(md_text: str) -> str | None:
    """Return the text between FULLTEXT_START and FULLTEXT_END (excl.
    the markers themselves). None if either marker is missing."""
    i = md_text.find(FULLTEXT_START)
    if i < 0:
        return None
    j = md_text.find(FULLTEXT_END, i + len(FULLTEXT_START))
    if j < 0:
        return None
    return md_text[i + len(FULLTEXT_START):j]


def _replace_fulltext_region(md_text: str, new_region: str) -> str:
    """Splice new_region between the fulltext markers, preserving
    everything else verbatim. Raises if markers missing."""
    i = md_text.find(FULLTEXT_START)
    j = md_text.find(FULLTEXT_END, i + len(FULLTEXT_START))
    if i < 0 or j < 0:
        raise ReSummarizeError("fulltext markers missing — cannot splice")
    before = md_text[:i + len(FULLTEXT_START)]
    after = md_text[j:]
    # Normalise padding: one newline after START, one before END.
    body = new_region.strip("\n")
    return f"{before}\n{body}\n{after}"


_SECTION_HEAD_RE = re.compile(
    r"^##\s+(\d+)\.\s*(.*?)\s*$", re.MULTILINE,
)


def _split_into_sections(region: str) -> list[str]:
    """Split fulltext region into SECTION_COUNT section bodies.

    A section starts at `## N. <title>` and runs until the next
    `## M.` heading or end-of-region. Returns a list in section-number
    order; if any section is missing, the list length will differ
    from SECTION_COUNT (caller handles the mismatch).
    """
    matches = list(_SECTION_HEAD_RE.finditer(region))
    if not matches:
        return []
    sections: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(region)
        sections.append((num, region[start:end].rstrip("\n")))
    sections.sort(key=lambda t: t[0])
    return [body for _, body in sections]


def _rejoin_sections(sections: list[str]) -> str:
    """Inverse of _split_into_sections: join section bodies with two
    blank lines between them (markdown convention)."""
    return "\n\n".join(s.rstrip("\n") for s in sections) + "\n"


def _run_new_summary_pass(
    kb_root: Path, paper_key: str, old_md_text: str,
    *, provider: str | None, model: str | None,
) -> list[str]:
    """Call kb-importer's fulltext pipeline to produce a fresh 7-section
    summary for this paper. Returns a list of 7 section bodies in the
    same shape _split_into_sections returns.

    Soft-dep: raises ReSummarizeError with a clear message if
    kb_importer isn't installed.

    v27: converts kb_importer's typed exceptions
    (FileNotFoundError for missing PDFs, BadRequestError for LLM
    400s, QuotaExhaustedError etc.) to ReSummarizeError with a
    `code` attribute so the re-read / events classifier can route
    on a tag instead of a substring.
    """
    try:
        from kb_importer.resummarize_adapter import run_new_summary
    except ImportError as e:
        raise ReSummarizeError(
            f"kb-importer is not installed; re-summarize needs it to "
            f"run the LLM pass. Install with `pip install -e ./kb_importer`. "
            f"({e})"
        )
    # Typed-error pass-through. kb_importer raises its own Exception
    # hierarchy; we translate at this boundary so everything upstream
    # only needs to understand ReSummarizeError + code.
    try:
        from kb_importer.summarize import (
            BadRequestError as _BadReq,
            PdfMissingError as _PdfMissing,
            QuotaExhaustedError as _Quota,
            SummarizerError as _SumErr,
        )
    except ImportError:
        # Shouldn't happen when resummarize_adapter imported, but be
        # defensive — use the base Exception as a sentinel class.
        _BadReq = _PdfMissing = _Quota = _SumErr = ()  # type: ignore[assignment]
    try:
        return run_new_summary(
            kb_root=kb_root, paper_key=paper_key, old_md_text=old_md_text,
            provider=provider, model=model,
        )
    except FileNotFoundError as e:
        raise ReSummarizeError(
            f"{paper_key}: PDF missing — {e}", code="pdf_missing",
        ) from e
    except _PdfMissing as e:  # type: ignore[misc]
        raise ReSummarizeError(
            f"{paper_key}: {e}", code="pdf_missing",
        ) from e
    except _BadReq as e:  # type: ignore[misc]
        raise ReSummarizeError(
            f"{paper_key}: {e}", code="bad_request",
        ) from e
    except _Quota as e:  # type: ignore[misc]
        # Quota is a provider-state signal, not a per-paper defect.
        # Surface as llm_other with explicit code so the caller (batch
        # re-read) can stop the run rather than move on to the next
        # paper only to hit the same quota.
        raise ReSummarizeError(
            f"{paper_key}: provider quota exhausted — {e}",
            code="quota",
        ) from e
    except _SumErr as e:  # type: ignore[misc]
        raise ReSummarizeError(
            f"{paper_key}: {e}", code="llm_other",
        ) from e


def _judge_sections(
    kb_root: Path, paper_key: str,
    old_sections: list[str], new_sections: list[str],
    *, provider: str | None, model: str | None,
) -> list[SectionVerdict]:
    """Run a short LLM judging pass that, for each of the 7 sections,
    compares old-vs-new and returns a verdict. When the two agree
    (byte-equal after whitespace normalisation), we short-circuit to
    verdict='tied' without an LLM call."""
    verdicts: list[SectionVerdict] = []
    pending_pairs: list[tuple[int, str, str]] = []  # (idx, old, new)
    for i, (old, new) in enumerate(zip(old_sections, new_sections)):
        if _text_equiv(old, new):
            verdicts.append(SectionVerdict(
                section=i + 1, verdict="tied",
                reason="byte-equivalent after whitespace norm",
            ))
        else:
            pending_pairs.append((i, old, new))

    if not pending_pairs:
        return verdicts

    # Soft-dep on kb_importer for the judge call too — it reuses the
    # provider config and API keys.
    try:
        from kb_importer.resummarize_adapter import judge_sections
    except ImportError as e:
        raise ReSummarizeError(
            f"kb-importer not installed: {e}"
        )
    judged = judge_sections(
        kb_root=kb_root, paper_key=paper_key,
        pairs=pending_pairs,
        provider=provider, model=model,
    )
    # judged is list of (idx, verdict, reason, new_content_if_chosen).
    by_idx = {j[0]: j for j in judged}
    for i in range(len(old_sections)):
        # Already "tied" from short-circuit?
        existing = next((v for v in verdicts if v.section == i + 1), None)
        if existing:
            continue
        info = by_idx.get(i)
        if info is None:
            # Judge didn't return for this section — treat as "old" so
            # we don't overwrite without explicit approval.
            verdicts.append(SectionVerdict(
                section=i + 1, verdict="old",
                reason="judge returned no verdict; defaulting to keep old",
            ))
            continue
        _, v, reason, new_content = info
        verdicts.append(SectionVerdict(
            section=i + 1, verdict=v, reason=reason,
            new_content=new_content if v == "new" else "",
        ))
    # Re-sort by section number so the report is ordered.
    verdicts.sort(key=lambda s: s.section)
    return verdicts


def _text_equiv(a: str, b: str) -> bool:
    """Whitespace-insensitive equality. LLMs emit minor whitespace
    variation (trailing spaces, blank-line count) between runs; we
    should NOT flag those as content differences."""
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    return norm(a) == norm(b)


def format_report(report: ReSummarizeReport) -> str:
    """Human-readable report for CLI stdout."""
    lines = [f"re-summarize {report.paper_key}:", ""]
    for v in report.verdicts:
        if v.verdict == "new":
            lines.append(f"  §{v.section}  UPDATED  — {v.reason or 'LLM judged new as correct'}")
        elif v.verdict == "old":
            lines.append(f"  §{v.section}  kept     — {v.reason or 'LLM judged old as correct'}")
        else:
            lines.append(f"  §{v.section}  tied     — {v.reason or 'identical after normalisation'}")
    lines.append("")
    lines.append(report.summary_line())
    if report.git_sha:
        lines.append(f"git: {report.git_sha}")
    return "\n".join(lines)
