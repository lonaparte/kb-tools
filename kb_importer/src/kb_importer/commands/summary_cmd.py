"""`kb-importer set-summary` and `kb-importer import-summaries`.

## Design

kb-importer does NOT talk to any LLM. Summaries are produced externally
(by the caller — typically an LLM agent orchestrating this CLI), and
kb-importer's job is just to shepherd the summary text into the
right paper md's fulltext region.

Two command shapes:

- **`set-summary <paper_key>`**: read summary text from stdin (or
  --from-file), write it into that paper's md fulltext region. For
  when the caller has already generated the summary and just wants it
  stored.

- **`import-summaries`**: scan Zotero for already-imported papers
  whose child notes contain "AI Summary" on the first line. Move those
  notes into the paper md's fulltext region. For migrating existing
  hand-written summaries from Zotero.

Both commands write the same frontmatter markers when successful:
- `fulltext_processed: true`
- `fulltext_processed_at: <ISO timestamp>`
- `fulltext_source: external | zotero_note`
- (for zotero_note:) `fulltext_source_note_keys: [note_key, ...]`

By default, papers already marked `fulltext_processed: true` are
skipped; use `--force` to overwrite.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from importlib import resources

from markdownify import markdownify as html_to_md

from ..config import Config
from ..md_builder import paper_md_path
from ..md_io import inject_fulltext, read_md
from ..state import imported_paper_keys
from ..zotero_reader import ZoteroReader

log = logging.getLogger(__name__)


# Matches a line whose content (after HTML-tag stripping and
# whitespace trim) contains "AI Summary" (case-insensitive). Used to
# identify summary-bearing Zotero child notes.
_AI_SUMMARY_RE = re.compile(r"ai[\s_-]*summary", re.IGNORECASE)


# Eligibility routing lives in kb_importer.eligibility. Note that
# `is_fulltext_eligible` returns True for books/theses (they're
# handled by the long pipeline), but set-summary / batch-set-summary
# accept only SHORT-pipeline types — the template the caller fills
# is the 7-section journal-article template, and asking users to
# write one for a 300-page book is a category error. Use the stricter
# `fulltext_mode(...) == "short"` check in this file instead.
from ..eligibility import (
    fulltext_mode as _fulltext_mode,
    NO_FULLTEXT_TYPES as NO_FULLTEXT_ITEM_TYPES,  # noqa: F401 (kept for backcompat display)
    LONG_PIPELINE_TYPES as _LONG_TYPES,
)


def _is_short_pipeline_eligible(item_type: str) -> bool:
    """True iff this item_type is a fit for the short-paper summary
    template (i.e. set-summary / batch-set-summary should accept it).
    Books / theses are rejected here because their content belongs
    in the long pipeline, not in a single 7-section summary.
    """
    return _fulltext_mode(item_type) == "short"


# Backcompat alias: previously `_is_fulltext_eligible` returned False
# for books. With the v22 eligibility split, the public function
# returns True for books (they have a pipeline — long). Within this
# file we want the old stricter semantics, so keep using
# `_is_short_pipeline_eligible`.
_is_fulltext_eligible = _is_short_pipeline_eligible


# ---------------------------------------------------------------------
# set-summary: accept summary text from caller, write to md
# ---------------------------------------------------------------------

def add_set_summary_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "set-summary",
        help="Write an externally-generated summary into a paper's md.",
    )
    p.add_argument(
        "paper_key",
        help="Zotero paper key whose md should receive the summary.",
    )
    p.add_argument(
        "--from-file",
        help="Read summary from this file instead of stdin.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Overwrite even if fulltext_processed is already true.",
    )
    p.set_defaults(func=run_set_summary)


def run_set_summary(args: argparse.Namespace, cfg: Config) -> int:
    md_path = paper_md_path(cfg.kb_root, args.paper_key)
    if not md_path.exists():
        print(f"Error: no md at {md_path}", file=sys.stderr)
        # The most common cause of this error is passing an ATTACHMENT
        # key instead of a PAPER key — the two keys coexist in Zotero
        # (storage/XYZ is an attachment key; paper md names use paper
        # keys). Reverse-lookup: is this key in any imported paper's
        # zotero_attachment_keys list?
        parent_paper = _find_paper_by_attachment_key(cfg, args.paper_key)
        if parent_paper:
            print(
                f"\nHint: {args.paper_key!r} looks like an ATTACHMENT key "
                f"(storage/ subdir name), not a paper key. It belongs to "
                f"paper {parent_paper!r}. Try:",
                file=sys.stderr,
            )
            print(
                f"  kb-importer set-summary {parent_paper} < your_summary.md",
                file=sys.stderr,
            )
        else:
            print(
                f"Import the paper first: "
                f"kb-importer import papers {args.paper_key}",
                file=sys.stderr,
            )
        return 2

    # Check already-processed.
    try:
        post = read_md(md_path)
        if post.metadata.get("fulltext_processed") and not args.force:
            print(
                f"Paper {args.paper_key} already has fulltext_processed=true; "
                f"use --force to overwrite.",
                file=sys.stderr,
            )
            return 3
    except Exception as e:
        print(f"Error reading {md_path}: {e}", file=sys.stderr)
        return 2

    # set-summary is the user-facing "manually provide a summary"
    # path. It accepts ONLY short-pipeline item types: the 7-section
    # template is designed for journal-article shape. Books and theses
    # are rejected here — they should go through the long pipeline
    # (longform.longform_ingest_paper), which generates per-chapter
    # thoughts instead.
    item_type = post.metadata.get("item_type", "")
    if not _is_fulltext_eligible(item_type):
        rejected = sorted(NO_FULLTEXT_ITEM_TYPES | _LONG_TYPES)
        print(
            f"Paper {args.paper_key} has item_type={item_type!r}, which is "
            f"not eligible for the set-summary / 7-section template. "
            f"(Rejected types: {rejected}. Books/theses go through the "
            f"long pipeline instead: `kb-importer import papers {args.paper_key} "
            f"--fulltext --longform`.) Refusing.",
            file=sys.stderr,
        )
        return 4

    # Load summary text.
    if args.from_file:
        try:
            summary_text = open(args.from_file, encoding="utf-8").read()
        except OSError as e:
            print(f"Error reading {args.from_file}: {e}", file=sys.stderr)
            return 2
    else:
        if sys.stdin.isatty():
            print(
                "Error: no --from-file and stdin is a terminal. "
                "Pipe summary text in, or pass --from-file.",
                file=sys.stderr,
            )
            return 2
        summary_text = sys.stdin.read()

    summary_text = summary_text.strip()
    if not summary_text:
        print("Error: summary text is empty.", file=sys.stderr)
        return 2

    if getattr(args, "dry_run", False):
        print(f"(dry-run) would write {len(summary_text)} chars to {md_path}")
        return 0

    inject_fulltext(
        md_path,
        summary_text,
        source_meta={
            "fulltext_processed": True,
            "fulltext_processed_at": _now_iso(),
            "fulltext_source": "external",
        },
    )
    print(f"✓ wrote summary to {md_path}")
    return 0


# ---------------------------------------------------------------------
# import-summaries: harvest AI Summary notes from Zotero
# ---------------------------------------------------------------------

def add_import_summaries_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "import-summaries",
        help=(
            "Scan Zotero for imported papers whose child notes contain "
            "'AI Summary'; migrate those notes into the paper md's "
            "fulltext region."
        ),
    )
    p.add_argument(
        "--only",
        metavar="PAPER_KEY",
        action="append",
        default=None,
        help=(
            "Limit to specific paper key(s). Can be given multiple times. "
            "If omitted, all imported papers are considered."
        ),
    )
    p.add_argument(
        "--force", action="store_true",
        help=(
            "Process papers even if fulltext_processed is already true "
            "(overwrites existing fulltext region)."
        ),
    )
    p.set_defaults(func=run_import_summaries)


def run_import_summaries(args: argparse.Namespace, cfg: Config) -> int:
    try:
        reader = ZoteroReader(cfg)
    except Exception as e:
        print(f"Error connecting to Zotero ({cfg.zotero_source_mode}): {e}",
              file=sys.stderr)
        return 2

    dry_run = getattr(args, "dry_run", False)

    # Which papers to consider.
    if args.only:
        candidate_keys = set(args.only)
    else:
        candidate_keys = imported_paper_keys(cfg)

    if not candidate_keys:
        print("No imported papers to process.")
        return 0

    total = len(candidate_keys)
    processed = 0      # papers whose fulltext was written this run
    skipped_processed = 0  # already had fulltext_processed=true
    skipped_no_match = 0   # no child note matched
    skipped_ineligible = 0 # item_type in NO_FULLTEXT_ITEM_TYPES
    failed = 0

    for pk in sorted(candidate_keys):
        md_path = paper_md_path(cfg.kb_root, pk)
        if not md_path.exists():
            log.warning("skip %s: no md at %s", pk, md_path)
            skipped_no_match += 1
            continue

        # Already-processed check.
        try:
            post = read_md(md_path)
        except Exception as e:
            log.warning("skip %s: couldn't read md (%s)", pk, e)
            failed += 1
            continue

        # Item types like book, thesis, report, webpage: skip regardless
        # of note content. These aren't research papers and our summary
        # template doesn't fit them.
        item_type = post.metadata.get("item_type", "")
        if not _is_fulltext_eligible(item_type):
            skipped_ineligible += 1
            log.debug("skip %s: item_type=%s (not fulltext-eligible)",
                      pk, item_type)
            continue

        if post.metadata.get("fulltext_processed") and not args.force:
            skipped_processed += 1
            continue

        # Fetch child notes from Zotero.
        try:
            item = reader.get_paper(pk)
        except Exception as e:
            log.warning("skip %s: couldn't fetch from Zotero (%s)", pk, e)
            failed += 1
            continue

        summary_notes = _select_ai_summary_notes(item.notes)
        if not summary_notes:
            skipped_no_match += 1
            continue

        # Concatenate the notes into the fulltext region.
        fulltext_body = _build_fulltext_from_notes(summary_notes)

        if dry_run:
            note_count = len(summary_notes)
            print(f"(dry-run) would migrate {note_count} note(s) → {pk}")
            processed += 1
            continue

        try:
            inject_fulltext(
                md_path,
                fulltext_body,
                source_meta={
                    "fulltext_processed": True,
                    "fulltext_processed_at": _now_iso(),
                    "fulltext_source": "zotero_note",
                    "fulltext_source_note_keys": [
                        n.key for n in summary_notes
                    ],
                },
            )
            # Re-render the paper md body. Why: inject_fulltext is a
            # surgical update that only touches the fulltext region, so
            # the Zotero Notes region still contains the SAME notes
            # we just migrated — i.e. the summary would appear twice
            # in the md. Re-rendering with the updated frontmatter
            # (which now includes fulltext_source_note_keys) causes
            # build_paper_md to exclude migrated notes from the
            # Zotero Notes section. Everything else (AI zone, kb_*,
            # fulltext we just wrote) is preserved by the normal
            # extract-preserved / inject-preserved machinery.
            _rerender_paper(cfg, reader, pk)
            print(f"✓ {pk}: migrated {len(summary_notes)} note(s)")
            processed += 1
        except Exception as e:
            log.exception("failed to inject summary for %s", pk)
            print(f"✗ {pk}: {e}", file=sys.stderr)
            failed += 1

    # Summary line.
    print()
    print(f"Processed: {processed}")
    print(f"Already had fulltext (skipped):  {skipped_processed}")
    print(f"No AI Summary note (skipped):    {skipped_no_match}")
    print(f"Ineligible item type (skipped):  {skipped_ineligible}")
    print(f"Failed:                          {failed}")
    print(f"Total considered:                {total}")

    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _select_ai_summary_notes(notes: list) -> list:
    """Return notes whose first non-empty line matches 'AI Summary'.

    Accepts HTML Zotero notes. Strips tags and checks case-insensitively.
    Multiple matches OK — returned sorted by dateAdded DESC (newest first).
    """
    matched = []
    for note in notes:
        first_line = _first_meaningful_line(note.html)
        if first_line and _AI_SUMMARY_RE.search(first_line):
            matched.append(note)
    # Sort newest first. Empty dateAdded sorts last.
    matched.sort(key=lambda n: n.date_added or "", reverse=True)
    return matched


def _first_meaningful_line(html: str) -> str:
    """Extract the first non-empty line of plaintext from HTML.

    Zotero notes are HTML, typically with `<p>...</p>` or `<h1>...</h1>`
    blocks. We treat each block-level element as a line boundary, then
    strip remaining inline tags, then pick the first non-empty line.
    """
    if not html:
        return ""
    # Replace closing block-level tags with newlines, so each paragraph
    # / heading starts on its own line even if the source HTML was all
    # on one line.
    text = re.sub(
        r"</\s*(p|h[1-6]|div|li|br|tr)\s*>",
        "\n",
        html,
        flags=re.IGNORECASE,
    )
    # Also treat self-closing <br/> as newlines.
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Now strip all remaining tags.
    text = re.sub(r"<[^>]+>", "", text)
    # Common HTML entities.
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _build_fulltext_from_notes(notes: list) -> str:
    """Concatenate multiple AI Summary notes into one fulltext body.

    Each note is converted from HTML to markdown, with a horizontal
    rule between consecutive notes so they remain visually separated.
    The AI Summary title line is preserved inside each note (serves as
    a marker per user preference).
    """
    parts = []
    for note in notes:
        md = html_to_md(note.html or "").strip()
        if not md:
            continue
        parts.append(md)
    # Horizontal-rule separator between multiple summaries.
    return "\n\n---\n\n".join(parts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rerender_paper(cfg: Config, reader: ZoteroReader, paper_key: str) -> None:
    """Rebuild a paper md from Zotero + current preserved content.

    Thin wrapper around import_cmd._process_paper so summary_cmd doesn't
    duplicate the import flow. Delegating keeps the "how to write a
    paper md" logic in one place.
    """
    # Local import to avoid a circular import at module load time.
    from .import_cmd import _process_paper
    _process_paper(cfg, reader, paper_key, dry_run=False)


def _find_paper_by_attachment_key(cfg: Config, needle: str) -> str | None:
    """Scan all imported paper mds, return the paper_key whose
    `zotero_attachment_keys` frontmatter list contains `needle`.

    Used to produce helpful error messages when users accidentally pass
    an attachment key where a paper key was expected. Phase 2 will
    have a proper SQLite reverse index for this; for now it's O(N)
    scan over all paper mds. Returns the FIRST match (in the rare
    case of duplicates, which shouldn't happen).
    """
    for pk in imported_paper_keys(cfg):
        md_path = paper_md_path(cfg.kb_root, pk)
        try:
            post = read_md(md_path)
            att_keys = post.metadata.get("zotero_attachment_keys", []) or []
            if needle in att_keys:
                return pk
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------
# show-template: print the bundled AI Summary prompt template
# ---------------------------------------------------------------------

# Filename of the template inside the kb_importer.templates package.
AI_SUMMARY_TEMPLATE_NAME = "ai_summary_prompt.md"


def add_show_template_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "show-template",
        help=(
            "Print the AI Summary prompt template bundled with kb-importer. "
            "Pipe it into an LLM agent to guide summary generation."
        ),
    )
    p.add_argument(
        "--path", action="store_true",
        help="Print the filesystem path to the template file instead of contents.",
    )
    p.set_defaults(func=run_show_template)


def run_show_template(args: argparse.Namespace, cfg: Config) -> int:
    """Print the template file shipped inside the package.

    Uses importlib.resources so it works whether kb-importer is
    installed via `pip install -e .` (file on disk) or as a regular
    wheel (file inside site-packages). If the user has edited the
    installed file they'll see the edits.
    """
    try:
        files = resources.files("kb_importer.templates")
        template_resource = files / AI_SUMMARY_TEMPLATE_NAME
    except (ModuleNotFoundError, AttributeError) as e:
        print(
            f"Error: could not locate the template package: {e}",
            file=sys.stderr,
        )
        return 2

    if args.path:
        # For `pip install -e` and regular wheels alike, `as_file`
        # gives us the path. For sdists inside zips it would extract
        # to a temp path — not our case, but be robust.
        try:
            with resources.as_file(template_resource) as p:
                print(p)
            return 0
        except Exception as e:
            print(f"Error resolving template path: {e}", file=sys.stderr)
            return 2

    try:
        text = template_resource.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"Error: template {AI_SUMMARY_TEMPLATE_NAME!r} not found in the "
            f"installed kb_importer.templates package. The install may be "
            f"broken.",
            file=sys.stderr,
        )
        return 2

    # Write to stdout without any extra framing — caller can pipe
    # directly into an LLM.
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0
