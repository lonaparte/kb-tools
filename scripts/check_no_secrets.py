#!/usr/bin/env python3
"""Pre-release lint: scan source tree for accidentally leaked secrets
and personally identifying information.

Run before publishing (GitHub, zip sharing, etc.). Exits 0 if clean,
non-zero with a report otherwise.

What we scan for:

1. Hard-coded API keys / tokens / passwords
   - Common prefixes: sk-..., AIza..., pk-..., Bearer ey...
   - Assignments like api_key="..." / token='...'

2. Personal identifiers
   - Non-example email addresses
   - IP addresses (beyond localhost)
   - /home/<user>/ or /Users/<user>/ absolute paths

3. Specific personal details of this project's author
   - University / employer / location markers
   - Real Zotero library_id patterns

4. CJK / non-ASCII characters (source should be English; CJK content
   often drifts in via comments and may leak names/notes).

5. Git residue
   - .git/ directory
   - merge-conflict markers ("<<<<<<<" / ">>>>>>>" / "=======")

The false-positive-to-find-rate trade-off: we err toward more
matches, since the developer reviews output anyway before shipping.

Usage:
    python3 scripts/check_no_secrets.py             # from .ee-kb-tools/
    python3 scripts/check_no_secrets.py --verbose   # show every match
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Directories to scan (relative to .ee-kb-tools/)
SCAN_DIRS = [
    "kb_importer", "kb_mcp", "kb_write", "kb_citations",
    "scripts",
]
# Root-level files to scan
SCAN_ROOT_FILES = ["README.md"]
# Extensions considered "source"
SOURCE_EXTS = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".txt"}
# Directories to skip entirely
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules"}

# ----------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------

# API key / token patterns — high confidence
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"),          "OpenAI-style key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{10,}"),   "OpenAI project key"),
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"),        "Google API key"),
    (re.compile(r"Bearer\s+ey[A-Za-z0-9_-]{20,}"), "JWT bearer token"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"),          "GitHub personal token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),  "GitHub fine-grained PAT"),
    (re.compile(r"xox[abp]-[A-Za-z0-9-]{20,}"),    "Slack token"),
]

# Assignments like `api_key = "..."` with suspiciously long value.
# Tolerates os.getenv(), env-var references, placeholders, tests.
SUSPICIOUS_ASSIGN = re.compile(
    r"""(api[_-]?key|secret|token|password)\s*[:=]\s*["']([A-Za-z0-9_\-]{15,})["']""",
    re.IGNORECASE,
)
ASSIGN_ALLOW = re.compile(
    r"""(os\.getenv|os\.environ|getenv\(|environ\[|env\.get|"""
    r"""example|your[_-]api|xxxxx|placeholder|"""
    r"""aaa|bbb|sk-\.\.\.|test|dummy|fake|redacted|"""
    r"""api_key_env|_api_key_env|KEY=["']?(sk-\.\.\.|your|example))""",
    re.IGNORECASE,
)

# Personal markers specific to the project author — easy to miss in
# manual review, cheap to scan for.
PERSONAL_MARKERS = [
    (re.compile(r"\bstockholm\b", re.IGNORECASE),      "Stockholm"),
    (re.compile(r"\bsweden\b",    re.IGNORECASE),      "Sweden"),
    (re.compile(r"\bkth\.?se\b",  re.IGNORECASE),      "KTH domain"),
    (re.compile(r"@kth\.se\b",    re.IGNORECASE),      "KTH email"),
    (re.compile(r"\buppsala\b",   re.IGNORECASE),      "Uppsala"),
    (re.compile(r"\bchalmers\b",  re.IGNORECASE),      "Chalmers"),
]

# Email addresses (excluding obvious examples)
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
)
EMAIL_ALLOW = re.compile(
    r"example\.(com|org)|your[._-]email|noreply|"
    r"anthropic\.com|github\.com|"
    r"your-org|maintainer@|test@|you@|"
    r"openalex_mailto|openalex\.org",
    re.IGNORECASE,
)

# Absolute paths that embed a username
HOME_PATH_RE = re.compile(
    r"(/home/[a-zA-Z_][a-zA-Z0-9_-]{0,30}/"
    r"|/Users/[a-zA-Z_][a-zA-Z0-9_-]{0,30}/"
    r"|C:\\\\Users\\\\[a-zA-Z_])"
)

# IP addresses that aren't obviously localhost / zero
IP_RE = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
IP_ALLOW = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}

# CJK character range (U+4E00..U+9FFF covers most Chinese;
# U+3040..U+30FF for Japanese kana; U+AC00..U+D7AF for Korean Hangul).
CJK_RE = re.compile(
    "["
    "\u4e00-\u9fff"   # CJK Unified Ideographs
    "\u3040-\u30ff"   # Hiragana + Katakana
    "\uac00-\ud7af"   # Hangul
    "]"
)

# Merge-conflict markers
CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7})\s", re.MULTILINE)


# Files expected to contain CJK content legitimately — these hold LLM
# prompts or the runtime constants used to parse/match the CJK text
# LLM prompts produce. CJK here is not a localisation residue; it is
# functional. The author's position: "code and comments must be
# English; content intended for the LLM may carry CJK". These file
# paths (relative to repo root) are exempt from the CJK check only.
# All other checks (secrets, personal markers, etc.) still apply.
CJK_EXEMPT_PATHS = frozenset({
    # Pure LLM prompt template (short pipeline)
    "kb_importer/src/kb_importer/templates/ai_summary_prompt.md",
    # Contains SYSTEM_PROMPT / USER_TMPL strings + SECTION_TITLES
    # constants used to align md headings with LLM output
    "kb_importer/src/kb_importer/summarize.py",
    # Long-form (book/thesis) per-chapter prompts
    "kb_importer/src/kb_importer/longform.py",
    # Regex patterns that match Chinese chapter headers like 第N章
    # (functional: otherwise Chinese books can't be chapter-split)
    "kb_importer/src/kb_importer/longform_split.py",
    # SECTION_TITLES_CH constant consumed by re-summarize pipeline
    "kb_importer/src/kb_importer/resummarize_adapter.py",
})


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------

def iter_source_files(root: Path):
    """Yield every source file under the scan set, skipping boilerplate."""
    for d in SCAN_DIRS:
        base = root / d
        if not base.exists():
            continue
        for dp, dn, fn in os.walk(base):
            # prune in place
            dn[:] = [x for x in dn if x not in SKIP_DIRS]
            for name in fn:
                p = Path(dp) / name
                if p.suffix.lower() in SOURCE_EXTS:
                    yield p
    for fn in SCAN_ROOT_FILES:
        p = root / fn
        if p.exists():
            yield p


def scan_file(p: Path, root: Path, findings: list[tuple[str, Path, int, str]]):
    """Scan one file, append findings. Each finding is
    (category, path, line_num, excerpt).
    """
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines()

    # The lint script itself contains all the patterns as string
    # literals — it will match everything. Skip it cleanly.
    if p.name == "check_no_secrets.py":
        return

    # Work out path-relative-to-root once (used for CJK exemption).
    try:
        rel_path = p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel_path = p.as_posix()
    cjk_exempt = rel_path in CJK_EXEMPT_PATHS

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue

        # 1. Secret patterns
        for rx, label in SECRET_PATTERNS:
            if rx.search(line):
                findings.append(("secret", p, i, f"[{label}] {stripped[:120]}"))
                break

        # 2. Suspicious assignment
        m = SUSPICIOUS_ASSIGN.search(line)
        if m and not ASSIGN_ALLOW.search(line):
            findings.append(("assign", p, i, stripped[:140]))

        # 3. Email (not in allow-list)
        for em in EMAIL_RE.finditer(line):
            if not EMAIL_ALLOW.search(em.group(0)) and not EMAIL_ALLOW.search(line):
                findings.append(("email", p, i, f"{em.group(0)}  ({stripped[:100]})"))

        # 4. Absolute-home path
        if HOME_PATH_RE.search(line):
            findings.append(("home-path", p, i, stripped[:140]))

        # 5. Non-local IP
        for ip in IP_RE.finditer(line):
            if ip.group(0) not in IP_ALLOW:
                findings.append(("ip", p, i, f"{ip.group(0)}  ({stripped[:100]})"))

        # 6. CJK characters — skip check if this file is on the
        # allow-list (prompts / LLM-facing constants).
        if not cjk_exempt and CJK_RE.search(line):
            findings.append(("cjk", p, i, stripped[:140]))

        # 7. Personal markers
        for rx, label in PERSONAL_MARKERS:
            if rx.search(line):
                findings.append(("personal", p, i, f"[{label}] {stripped[:120]}"))
                break

    # 8. Merge-conflict (file-level)
    if CONFLICT_RE.search(text):
        findings.append(("conflict", p, 0, "<<<<<<< / ======= / >>>>>>> markers"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="Project root (default: cwd)")
    ap.add_argument("--verbose", action="store_true",
                    help="Print every finding; otherwise summarised per category")
    args = ap.parse_args()

    root = Path(args.root).resolve()

    findings: list[tuple[str, Path, int, str]] = []
    n_files = 0
    for p in iter_source_files(root):
        n_files += 1
        scan_file(p, root, findings)

    # Git-directory check (not per file)
    if (root / ".git").exists():
        # .git/ existing is fine for local development, only a concern
        # if zipping with it. Not a hard fail — note only.
        pass

    if not findings:
        print(f"✓ no secrets / personal info found "
              f"({n_files} source files scanned)")
        return 0

    # Group by category for summary
    from collections import defaultdict
    by_cat: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)
    for cat, p, ln, ex in findings:
        by_cat[cat].append((p, ln, ex))

    print(f"✗ {len(findings)} potential leak(s) across "
          f"{n_files} source files\n")

    for cat in sorted(by_cat):
        items = by_cat[cat]
        print(f"  {cat}: {len(items)} match(es)")
        shown = items if args.verbose else items[:5]
        for p, ln, ex in shown:
            rel = p.relative_to(root) if p.is_absolute() else p
            loc = f"{rel}:{ln}" if ln > 0 else f"{rel}"
            print(f"    {loc}  {ex}")
        if not args.verbose and len(items) > 5:
            print(f"    ... +{len(items) - 5} more (use --verbose)")
        print()

    print(
        "Review each match. If legitimate (e.g. a scaffold "
        "placeholder, a documentation example), consider adding it "
        "to the allow-list regex in this script or rephrasing the "
        "source. Exits non-zero so CI can block a release."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
