"""`kb-write doctor`: scan the KB for rule violations, optionally fix
what's safely fixable.

Checks performed (see AGENT-WRITE-RULES.md):

- A. AI-zone markers: present, exactly one of each, in order
     (papers/ and topics/standalone-note/).
- B. Scaffold files present at KB root (README, CLAUDE, AGENTS,
     AGENT-WRITE-RULES).
- C. `.agent-prefs/README.md` present if .agent-prefs/ exists.
- D. Thought slugs match YYYY-MM-DD-name pattern.
- E. Topic slugs match kebab-case.
- F. Preference files have sensible frontmatter (scope, priority).
- G. kb_refs entries are well-formed paths AND resolve to an
     existing md file (dangling refs reported as INFO, not warning —
     forward references to unimported papers are sometimes intentional).
- H. Frontmatter field types: kb_tags / authors / kb_refs are lists
     of strings, kind / title / doi / zotero_key are strings,
     year is int, fulltext_processed is bool. Catches YAML typos
     like `kb_tags: "gfm"` (string not list) or `kb_tags: [1, 2]`
     (non-string elements) before they crash downstream consumers.
- I. List-field duplicates: kb_refs, kb_tags, authors should contain
     each value at most once. Duplicates don't crash anything but
     clutter the index and make diffs noisy. (v0.28.0)

`--fix` auto-repairs, in order of confidence:
  - B + C: creates missing scaffold files (idempotent).
  - A: appends missing AI-zone markers ONLY when both markers absent
    (never half-present; those are reported, not touched).
  - I: rewrites frontmatter with duplicates removed (order-preserved,
    first occurrence wins).
D/E/F/G/H are reported but not auto-fixed — they need human judgement
(D/E slug rename, F priority value, G dangling-ref intent, H type
coercion). For D specifically: run `kb-write migrate-slugs` to
canonicalise pre-v24 slug violations in a dedicated tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from ..atomic import atomic_write
from ..config import WriteContext
from ..rules import (
    RuleViolation, validate_thought_slug, validate_topic_slug,
    validate_kb_ref_entry,
)
from ..zones import (
    ensure_zone, find_zone, ZoneError,
    AI_ZONE_START, AI_ZONE_END,
)
from ..prompts.renderer import render_all


@dataclass
class Finding:
    severity: str                # "error" | "warning" | "info"
    category: str                # short tag e.g. "ai-zone", "slug"
    path: str                    # KB-relative path
    message: str
    auto_fixable: bool = False


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)  # descriptive strings
    scanned: int = 0

    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)


def doctor(
    ctx: WriteContext,
    *,
    fix: bool = False,
) -> DoctorReport:
    """Scan the KB and report (optionally repair) violations.

    Does NOT do git operations — doctor is observational unless you
    pass fix=True. Fixes that modify md are atomic-written; we DON'T
    auto-commit repairs (user should review `git diff` and commit
    manually).
    """
    report = DoctorReport()
    kb_root = ctx.kb_root

    _check_scaffold(kb_root, report, fix=fix)
    _check_parse_errors(kb_root, report)
    _check_papers(kb_root, report, fix=fix)
    _check_notes(kb_root, report, fix=fix)
    _check_thoughts(kb_root, report)
    _check_topics(kb_root, report)
    _check_prefs(kb_root, report)
    _check_refs_in_all(kb_root, report)
    _check_frontmatter_types(kb_root, report)
    _check_list_duplicates(kb_root, report, fix=fix)
    _check_revisits_markers(kb_root, report)

    return report


# ----------------------------------------------------------------------
# Revisits region (1.3.0): verify start/end markers pair cleanly and
# every revisit-block has a matching close. A malformed Revisits
# region would cause re_summarize --mode append to refuse its splice
# (by design) and the indexer to ingest broken boundaries.
# ----------------------------------------------------------------------

_REVISITS_START = "<!-- kb-revisits-start -->"
_REVISITS_END = "<!-- kb-revisits-end -->"
_REVISIT_BLOCK_OPEN_TAG = "<!-- kb-revisit-block"
_REVISIT_BLOCK_CLOSE = "<!-- /kb-revisit-block -->"


def _check_revisits_markers(kb_root: Path, report: DoctorReport) -> None:
    """For every paper md: if the file mentions the revisits start /
    end / block markers, they must balance and pair correctly.

    Only scans papers/; the revisits region doesn't appear anywhere
    else in the KB.
    """
    papers_dir = kb_root / "papers"
    if not papers_dir.exists():
        return
    for md in sorted(papers_dir.glob("*.md")):
        if md.name.startswith("."):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = md.relative_to(kb_root).as_posix()

        n_start = text.count(_REVISITS_START)
        n_end   = text.count(_REVISITS_END)
        n_open  = text.count(_REVISIT_BLOCK_OPEN_TAG)
        n_close = text.count(_REVISIT_BLOCK_CLOSE)

        # Skip files that have no revisits at all.
        if (n_start, n_end, n_open, n_close) == (0, 0, 0, 0):
            continue

        if n_start != 1 or n_end != 1:
            report.findings.append(Finding(
                severity="error", category="revisits",
                path=rel,
                message=(
                    f"revisits region markers unbalanced: "
                    f"start×{n_start}, end×{n_end} (must be exactly "
                    f"one of each). Inspect the md and restore markers."
                ),
                auto_fixable=False,
            ))
            continue

        # Region found. Start must come before end.
        i = text.find(_REVISITS_START)
        j = text.find(_REVISITS_END, i + len(_REVISITS_START))
        if j < 0:
            report.findings.append(Finding(
                severity="error", category="revisits",
                path=rel,
                message="revisits end marker appears before start marker",
                auto_fixable=False,
            ))
            continue

        if n_open != n_close:
            report.findings.append(Finding(
                severity="error", category="revisits",
                path=rel,
                message=(
                    f"revisit-block open/close markers unbalanced "
                    f"(open×{n_open}, close×{n_close})"
                ),
                auto_fixable=False,
            ))


# ----------------------------------------------------------------------
# Parse errors: every .md under the known content subdirs must have
# valid frontmatter. Downstream checks silently skip unparseable
# files; without this check, a broken YAML would not surface anywhere.
# 0.29.8: added after acceptance-test uncovered that doctor reported
# "0 findings" on a thought with unterminated YAML brackets.
# ----------------------------------------------------------------------

def _check_parse_errors(kb_root: Path, report: DoctorReport) -> None:
    for subdir in ("papers", "topics/standalone-note",
                   "topics/agent-created", "thoughts", ".agent-prefs"):
        d = kb_root / subdir
        if not d.exists():
            continue
        for md in sorted(d.rglob("*.md")):
            if md.name.startswith("."):
                continue
            # README files under subdirs (e.g. .agent-prefs/README.md)
            # are documentation, not content — skip them.
            if md.name.lower() == "readme.md":
                continue
            rel = md.relative_to(kb_root).as_posix()
            try:
                frontmatter.load(str(md))
            except Exception as e:
                report.findings.append(Finding(
                    severity="error", category="parse-error", path=rel,
                    message=f"could not parse frontmatter: {e}",
                    auto_fixable=False,
                ))


# ----------------------------------------------------------------------
# B + C: scaffold + prefs README
# ----------------------------------------------------------------------

def _check_scaffold(kb_root: Path, report: DoctorReport, *, fix: bool) -> None:
    rendered = render_all()
    required_rendered = set(rendered.keys())  # CLAUDE.md, AGENTS.md, README.md
    required_static = {"AGENT-WRITE-RULES.md", ".agent-prefs/README.md"}

    # B: rendered files
    for name in required_rendered:
        if not (kb_root / name).exists():
            report.findings.append(Finding(
                severity="error",
                category="scaffold",
                path=name,
                message=f"missing agent-discovery file {name!r}; "
                        "run `kb-write init` to create it.",
                auto_fixable=True,
            ))
            if fix:
                atomic_write(kb_root / name, rendered[name])
                report.fixed.append(f"created {name}")

    # C + B static files
    for name in required_static:
        path = kb_root / name
        if not path.exists():
            report.findings.append(Finding(
                severity="warning" if name.startswith(".") else "error",
                category="scaffold",
                path=name,
                message=f"missing {name!r}; run `kb-write init`.",
                auto_fixable=True,
            ))
            if fix:
                from .init import init_kb
                init_kb(kb_root)  # idempotent; only creates what's missing
                report.fixed.append(f"ran init to create {name}")
                break  # init creates all missing; stop the loop


# ----------------------------------------------------------------------
# A: AI-zone markers
# ----------------------------------------------------------------------

def _check_papers(kb_root: Path, report: DoctorReport, *, fix: bool) -> None:
    papers_dir = kb_root / "papers"
    if not papers_dir.exists():
        return
    for md in sorted(papers_dir.glob("*.md")):
        if md.name.startswith("."):
            continue
        report.scanned += 1
        _check_ai_zone(kb_root, md, report, fix=fix)


def _check_notes(kb_root: Path, report: DoctorReport, *, fix: bool) -> None:
    # v26: standalone Zotero notes live under topics/standalone-note/
    # (was zotero-notes/ in v25). Content at the legacy location is
    # flagged by _check_deprecated_v26_paths instead; we don't double-
    # scan it here.
    notes_dir = kb_root / "topics" / "standalone-note"
    if not notes_dir.exists():
        return
    for md in sorted(notes_dir.glob("*.md")):
        if md.name.startswith("."):
            continue
        report.scanned += 1
        _check_ai_zone(kb_root, md, report, fix=fix)


def _check_ai_zone(
    kb_root: Path, md: Path, report: DoctorReport, *, fix: bool,
) -> None:
    rel = md.relative_to(kb_root).as_posix()
    text = md.read_text(encoding="utf-8")
    n_start = text.count(AI_ZONE_START)
    n_end = text.count(AI_ZONE_END)

    if n_start == 1 and n_end == 1:
        try:
            find_zone(text)
            return  # OK
        except ZoneError as e:
            report.findings.append(Finding(
                severity="error", category="ai-zone", path=rel,
                message=f"AI zone malformed: {e}", auto_fixable=False,
            ))
            return

    if n_start == 0 and n_end == 0:
        # Completely missing — safe to append.
        report.findings.append(Finding(
            severity="warning", category="ai-zone", path=rel,
            message="AI zone markers missing; will be appended if --fix.",
            auto_fixable=True,
        ))
        if fix:
            atomic_write(md, ensure_zone(text))
            report.fixed.append(f"appended AI zone markers in {rel}")
        return

    # Asymmetric / duplicated — dangerous to repair automatically.
    report.findings.append(Finding(
        severity="error", category="ai-zone", path=rel,
        message=(
            f"AI zone markers malformed: {n_start} start(s), {n_end} end(s). "
            "Fix manually — auto-repair refused to avoid data loss."
        ),
        auto_fixable=False,
    ))


# ----------------------------------------------------------------------
# D + E: slug conventions
# ----------------------------------------------------------------------

def _check_thoughts(kb_root: Path, report: DoctorReport) -> None:
    d = kb_root / "thoughts"
    if not d.exists():
        return
    for md in sorted(d.glob("*.md")):
        report.scanned += 1
        try:
            validate_thought_slug(md.stem)
        except RuleViolation as e:
            report.findings.append(Finding(
                severity="warning", category="slug",
                path=md.relative_to(kb_root).as_posix(),
                message=(
                    f"{e}  (Tip: run `kb-write migrate-slugs` to "
                    f"bulk-rename pre-v24 slug violations.)"
                ),
                auto_fixable=False,
            ))


def _check_topics(kb_root: Path, report: DoctorReport) -> None:
    # v26: AI-generated topics live under topics/agent-created/
    # (was top-level topics/<slug>.md in v25).
    d = kb_root / "topics" / "agent-created"
    if not d.exists():
        return
    for md in sorted(d.rglob("*.md")):
        report.scanned += 1
        slug = md.relative_to(d).with_suffix("").as_posix()
        try:
            validate_topic_slug(slug)
        except RuleViolation as e:
            report.findings.append(Finding(
                severity="warning", category="slug",
                path=md.relative_to(kb_root).as_posix(),
                message=str(e), auto_fixable=False,
            ))


# ----------------------------------------------------------------------
# F: pref frontmatter sanity
# ----------------------------------------------------------------------

def _check_prefs(kb_root: Path, report: DoctorReport) -> None:
    d = kb_root / ".agent-prefs"
    if not d.exists():
        return
    for md in sorted(d.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        report.scanned += 1
        rel = md.relative_to(kb_root).as_posix()
        try:
            post = frontmatter.load(str(md))
        except Exception as e:
            report.findings.append(Finding(
                severity="error", category="pref-frontmatter", path=rel,
                message=f"could not parse frontmatter: {e}",
            ))
            continue
        fm = post.metadata
        if not fm.get("scope"):
            report.findings.append(Finding(
                severity="info", category="pref-frontmatter", path=rel,
                message="missing `scope` frontmatter (defaulting to 'global').",
            ))
        try:
            pri = int(fm.get("priority", 50))
            if not 0 <= pri <= 100:
                raise ValueError()
        except (ValueError, TypeError):
            report.findings.append(Finding(
                severity="warning", category="pref-frontmatter", path=rel,
                message=f"`priority` should be 0-100, got {fm.get('priority')!r}.",
            ))


# ----------------------------------------------------------------------
# G: kb_refs sanity across all node types
# ----------------------------------------------------------------------

def _check_refs_in_all(kb_root: Path, report: DoctorReport) -> None:
    # First pass: collect all local node identities so we can resolve
    # kb_refs to a "does it exist" answer. A ref like "papers/ABCD1234"
    # resolves to kb_root/papers/ABCD1234.md, etc.
    existing: set[str] = set()
    for subdir in ("papers", "topics/standalone-note", "topics/agent-created", "thoughts"):
        d = kb_root / subdir
        if not d.exists():
            continue
        for md in d.rglob("*.md"):
            rel = md.relative_to(kb_root).as_posix()
            existing.add(rel[:-3] if rel.endswith(".md") else rel)

    for subdir in ("papers", "topics/standalone-note", "topics/agent-created", "thoughts"):
        d = kb_root / subdir
        if not d.exists():
            continue
        for md in sorted(d.rglob("*.md")):
            rel = md.relative_to(kb_root).as_posix()
            try:
                post = frontmatter.load(str(md))
            except Exception:
                continue
            refs = post.metadata.get("kb_refs") or []
            if not isinstance(refs, list):
                report.findings.append(Finding(
                    severity="warning", category="kb_refs", path=rel,
                    message=f"kb_refs should be a list, got {type(refs).__name__}",
                ))
                continue
            for entry in refs:
                try:
                    if isinstance(entry, str):
                        validate_kb_ref_entry(entry)
                    else:
                        raise RuleViolation(f"non-string entry: {entry!r}")
                except RuleViolation as e:
                    report.findings.append(Finding(
                        severity="warning", category="kb_refs", path=rel,
                        message=f"bad kb_refs entry: {e}",
                    ))
                    continue
                # Dangling-ref check: does the target md exist?
                # Report as INFO (not warning) — forward references
                # to papers you haven't imported yet are sometimes
                # intentional (you'll import later; the ref pre-records
                # the intent). Surface them so you can decide.
                if isinstance(entry, str) and entry not in existing:
                    report.findings.append(Finding(
                        severity="info", category="dangling-ref",
                        path=rel,
                        message=f"kb_refs entry {entry!r} has no "
                                f"matching md file in the KB",
                    ))


# ----------------------------------------------------------------------
# H: frontmatter field type checks
# ----------------------------------------------------------------------

# Fields whose frontmatter value is expected to have a specific shape.
# (field_name, expected_kind, description_for_error_message)
# `expected_kind` is one of:
#   "list[str]"  — YAML sequence of strings
#   "str"        — YAML scalar string
#   "int"        — YAML scalar int
#   "bool"       — YAML scalar bool
# Matching is by `isinstance` with a tolerance for common confusions
# (e.g. kind: paper accepted as str; kind: 1 rejected).
_EXPECTED_FRONTMATTER_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("kb_tags",   "list[str]", "list of string tags"),
    ("kb_refs",   "list[str]", "list of ref paths like 'papers/ABCD'"),
    ("authors",   "list[str]", "list of author name strings"),
    ("kind",      "str",       "string like 'paper', 'thought', 'topic'"),
    ("title",     "str",       "string title"),
    ("year",      "int",       "integer year"),
    ("fulltext_processed", "bool", "bool (true/false)"),
    ("doi",       "str",       "string DOI"),
    ("zotero_key", "str",      "string Zotero key"),
    ("citation_key", "str",    "string citation key"),
)


def _shape_matches(value, expected: str) -> bool:
    """Return True if `value` satisfies the `expected` shape.
    Handles the four kinds used in _EXPECTED_FRONTMATTER_SHAPES.
    None is accepted as "missing" by the caller; this function is
    only called on non-None values.
    """
    if expected == "str":
        return isinstance(value, str)
    if expected == "int":
        # bool is a subclass of int — reject bools as ints here to
        # catch `year: true` typos.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "bool":
        return isinstance(value, bool)
    if expected == "list[str]":
        if not isinstance(value, list):
            return False
        return all(isinstance(x, str) for x in value)
    return True  # unknown shape: don't fail open, but don't warn


def _check_frontmatter_types(
    kb_root: Path, report: DoctorReport,
) -> None:
    """Scan every paper/thought/topic/note md for malformed
    frontmatter field types (e.g. `kb_tags: "not-a-list"` or
    `authors: [1, 2]`). v24 third-party report: doctor silently let
    these through, then the indexer or a downstream consumer would
    crash or mis-render far from the root cause. Catching them here
    keeps the error close to the bad md.

    Only reports — does not auto-fix. Fixing frontmatter types
    requires guessing what the user meant, which the user should do
    themselves.
    """
    for subdir in ("papers", "topics/standalone-note", "topics/agent-created", "thoughts"):
        d = kb_root / subdir
        if not d.exists():
            continue
        for md in sorted(d.rglob("*.md")):
            rel = md.relative_to(kb_root).as_posix()
            try:
                post = frontmatter.load(str(md))
            except Exception:
                # Parse errors are surfaced elsewhere (A-check may
                # already have flagged this); don't double-report.
                continue

            for field_name, expected, desc in _EXPECTED_FRONTMATTER_SHAPES:
                if field_name not in post.metadata:
                    continue
                value = post.metadata[field_name]
                if value is None:
                    continue  # YAML null is treated as absent
                if not _shape_matches(value, expected):
                    # Build a short preview of what we actually got.
                    # For lists, show the first element's type so the
                    # user can find the culprit quickly.
                    if expected == "list[str]" and isinstance(value, list):
                        offending = next(
                            (f"index {i}: {type(x).__name__}"
                             for i, x in enumerate(value)
                             if not isinstance(x, str)),
                            f"{type(value).__name__}",
                        )
                        actual_desc = f"list with non-string entry ({offending})"
                    else:
                        actual_desc = type(value).__name__
                    report.findings.append(Finding(
                        severity="warning",
                        category="frontmatter-type",
                        path=rel,
                        message=(
                            f"frontmatter field {field_name!r}: "
                            f"expected {desc}, got {actual_desc}. "
                            f"Value: {value!r:.80}"
                        ),
                    ))


# ----------------------------------------------------------------------
# I: list-field duplicates (kb_refs, kb_tags, authors)
# ----------------------------------------------------------------------

# Fields where we dedup duplicates as a high-confidence fix. These are
# all list-of-string fields where a value appearing twice is never
# meaningful — list membership is set semantics in our model.
_DEDUPABLE_LIST_FIELDS: tuple[str, ...] = ("kb_refs", "kb_tags", "authors")


def _dedup_preserve_order(items: list) -> tuple[list, list]:
    """Return (deduped_list, removed_duplicates).

    Preserves first-occurrence order. `removed_duplicates` is the list
    of (index, value) pairs removed, for display in the finding.
    """
    seen: set = set()
    out: list = []
    removed: list = []
    for i, x in enumerate(items):
        # Use a hashable key; for strings this is just x. Mixed-type
        # lists are caught by _check_frontmatter_types; here we're
        # only called on lists-of-strings.
        try:
            key = x
            if key in seen:
                removed.append((i, x))
                continue
            seen.add(key)
        except TypeError:
            # Unhashable (shouldn't happen for str) — keep as-is.
            pass
        out.append(x)
    return out, removed


def _check_list_duplicates(
    kb_root: Path, report: DoctorReport, *, fix: bool,
) -> None:
    """Scan every md for duplicate entries in kb_refs / kb_tags /
    authors. Reports a warning per field per file; --fix rewrites
    the frontmatter with duplicates removed (first occurrence wins,
    order preserved).

    Duplicates don't break anything at runtime (downstream callers
    typically set()-ify the list before use) but clutter diffs and
    make the indexer log lines twice. Safe to auto-dedupe because
    list-field semantics in this KB are set-like.

    Malformed lists (non-list, non-string elements) are handled by
    _check_frontmatter_types and skipped here to avoid double-reporting
    or touching a file that still needs a manual type fix.
    """
    for subdir in (
        "papers", "topics/standalone-note",
        "topics/agent-created", "thoughts",
    ):
        d = kb_root / subdir
        if not d.exists():
            continue
        for md in sorted(d.rglob("*.md")):
            rel = md.relative_to(kb_root).as_posix()
            try:
                post = frontmatter.load(str(md))
            except Exception:
                continue

            changed_fields: list[tuple[str, list, list]] = []
            for field_name in _DEDUPABLE_LIST_FIELDS:
                value = post.metadata.get(field_name)
                if not isinstance(value, list):
                    continue
                if not all(isinstance(x, str) for x in value):
                    # Non-string entries — defer to type-check finding.
                    continue
                deduped, removed = _dedup_preserve_order(value)
                if not removed:
                    continue
                # Find a human summary of the duplicates.
                dup_values = sorted({v for _, v in removed})
                report.findings.append(Finding(
                    severity="warning",
                    category="list-duplicates",
                    path=rel,
                    message=(
                        f"{field_name} has {len(removed)} duplicate "
                        f"entry/entries: "
                        f"{', '.join(repr(v) for v in dup_values[:5])}"
                        f"{'...' if len(dup_values) > 5 else ''}. "
                        f"Run with --fix to dedupe (first occurrence kept)."
                    ),
                    auto_fixable=True,
                ))
                changed_fields.append((field_name, value, deduped))

            if fix and changed_fields:
                # Rewrite the md with deduped lists. We build a fresh
                # frontmatter dict (not using merge_kb_fields — we want
                # SHRINK semantics, not UNION).
                new_fm = dict(post.metadata)
                for field_name, _before, after in changed_fields:
                    new_fm[field_name] = after
                try:
                    new_post = frontmatter.Post(post.content, **new_fm)
                    text = frontmatter.dumps(new_post)
                    if not text.endswith("\n"):
                        text += "\n"
                    atomic_write(md, text)
                    for field_name, before, after in changed_fields:
                        report.fixed.append(
                            f"deduped {field_name} in {rel} "
                            f"({len(before)} → {len(after)})"
                        )
                except Exception as e:
                    # Don't mask the finding — just record that the
                    # fix itself failed.
                    report.findings.append(Finding(
                        severity="error",
                        category="list-duplicates",
                        path=rel,
                        message=(
                            f"dedupe rewrite failed: "
                            f"{type(e).__name__}: {e}"
                        ),
                    ))


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

def format_report(report: DoctorReport) -> str:
    """Human-readable report. Ordered: errors, warnings, info."""
    lines = [
        f"kb-write doctor: scanned {report.scanned} md files, "
        f"{len(report.findings)} finding(s)."
    ]
    if not report.findings:
        lines.append("  ✓ no issues found.")
        if report.fixed:
            lines.append("")
            lines.append("Fixes applied:")
            for f in report.fixed:
                lines.append(f"  • {f}")
        return "\n".join(lines)

    by_sev: dict[str, list[Finding]] = {"error": [], "warning": [], "info": []}
    for f in report.findings:
        by_sev.setdefault(f.severity, []).append(f)

    for sev, icon in (("error", "✗"), ("warning", "⚠"), ("info", "ℹ")):
        items = by_sev.get(sev, [])
        if not items:
            continue
        lines.append("")
        lines.append(f"{sev.upper()}S ({len(items)}):")
        for f in items:
            fixable = "  [fixable]" if f.auto_fixable else ""
            lines.append(f"  {icon} [{f.category}] {f.path}{fixable}")
            lines.append(f"    {f.message}")

    if report.fixed:
        lines.append("")
        lines.append("Fixes applied:")
        for f in report.fixed:
            lines.append(f"  • {f}")

    if any(f.auto_fixable for f in report.findings) and not report.fixed:
        lines.append("")
        lines.append("Run `kb-write doctor --fix` to auto-repair fixable items.")

    return "\n".join(lines)
