"""RelatedToRecentSelector — papers related (by kb_refs / citation) to
recently-touched papers.

Motivation: when the user has been actively reading paper X, they
probably care more about re-reading X's references and citers than
random other papers. This selector surfaces those "nearby in the
graph" papers.

Two-step process:

  Step 1 — SEEDS (recently touched papers)
    Signals (union):
      - git log --since="<anchor_days> days ago" for papers/*.md
      - audit.log (JSONL) entries within window whose `target`
        points at a papers/*.md file
      - papers.md_mtime > (now - anchor_days * 86400)

  Step 2 — EXPAND (graph neighbours of seeds)
    For each seed, consult `links` table:
      neighbours += dst_key where src_type=paper, src_key=seed,
                    origin in edge_kinds
      neighbours += src_key where dst_type=paper, dst_key=seed,
                    origin in edge_kinds

    Exclude the seeds themselves (those are "already read").

    Rank: prefer neighbours pointed at by more distinct seeds
    (= higher "relevance to my recent reading"). Tie-break random.

Graceful degradation:
  - kb_mcp not installed → can't read links → returns empty
    (re-read's fallback kicks in)
  - git not present → skip that signal, still use audit + mtime
  - audit.log missing or malformed → skip that signal
  - links table empty (kb_citations never run) → use only kb_refs
    edges (origin='frontmatter' / 'wikilink' / 'mdlink')
"""
from __future__ import annotations

import random
import subprocess
import time
from pathlib import Path

from .base import PaperInfo


class RelatedToRecentSelector:
    name = "related-to-recent"
    description = (
        "Papers related (via kb_refs or citation edges) to papers "
        "the user recently touched. Defaults: anchor_days=14, "
        "edge_kinds=kb_ref,citation, fallback=unread-first."
    )
    ACCEPTED_KWARGS = frozenset({"anchor_days", "edge_kinds", "fallback"})

    def select(
        self,
        candidates: list[PaperInfo],
        *,
        count: int,
        kb_root: Path,
        seed: int | None = None,
        **kwargs,
    ) -> list[str]:
        if not candidates:
            return []

        # Parse selector-args with defaults. Be strict on typing:
        # silently coercing "anchor_days=abc" → 14 hides user errors.
        anchor_raw = kwargs.get("anchor_days", "14")
        try:
            anchor_days = int(anchor_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"related-to-recent: anchor_days must be integer, got "
                f"{anchor_raw!r}"
            )
        if anchor_days <= 0:
            raise ValueError(
                f"related-to-recent: anchor_days must be positive, got "
                f"{anchor_days}"
            )
        edge_kinds = _parse_list(
            kwargs.get("edge_kinds", "kb_ref,citation")
        )
        fallback_name = kwargs.get("fallback", "unread-first")

        # ----- Step 1: seeds ---------------------------------------
        seeds = _collect_seeds(kb_root, anchor_days)

        # ----- Step 2: expand --------------------------------------
        # Returns Counter-like dict: neighbour_key → number of seeds
        # pointing at it (higher = more related).
        neighbour_counts = _expand_neighbours(kb_root, seeds, edge_kinds)
        # Strip seeds out of the result (we want RELATED, not the
        # seeds themselves).
        for s in seeds:
            neighbour_counts.pop(s, None)

        # Intersect with candidates (only suggest papers actually
        # in the pool re-read is considering — papers with a paper_key
        # in the pool).
        cand_keys = {c.paper_key for c in candidates}
        scored = [
            (neighbour_counts[k], k) for k in neighbour_counts
            if k in cand_keys
        ]
        # Sort by count desc, tie-break random.
        rng = random.Random(seed)
        scored.sort(key=lambda t: (-t[0], rng.random()))
        chosen = [k for _, k in scored[:count]]

        # ----- Step 3: fallback if short ---------------------------
        if len(chosen) < count:
            extras_needed = count - len(chosen)
            chosen_set = set(chosen)
            # Pull fallback selector from registry and top up.
            try:
                from .registry import REGISTRY
                fallback_selector = REGISTRY.get(fallback_name)
            except ImportError:
                fallback_selector = None
            if fallback_selector is None:
                # Unknown fallback or registry unavailable → warn on
                # stderr and random-sample from what's left. Silently
                # using a different strategy would hide a typo in the
                # user's CLI args.
                import sys
                print(
                    f"warning: related-to-recent fallback={fallback_name!r} "
                    f"not a registered selector; using random fallback",
                    file=sys.stderr,
                )
                remaining = [
                    c for c in candidates if c.paper_key not in chosen_set
                ]
                if remaining:
                    extras = rng.sample(
                        remaining, min(extras_needed, len(remaining))
                    )
                    chosen.extend(c.paper_key for c in extras)
            else:
                # Run the fallback selector against candidates MINUS
                # what we already chose; merge results.
                remaining = [
                    c for c in candidates if c.paper_key not in chosen_set
                ]
                extras = fallback_selector.select(
                    remaining,
                    count=extras_needed,
                    kb_root=kb_root,
                    seed=seed,
                )
                chosen.extend(extras)

        # Dedupe while preserving order (belt-and-braces if fallback
        # happens to return already-chosen keys somehow).
        seen: set[str] = set()
        deduped: list[str] = []
        for k in chosen:
            if k not in seen:
                seen.add(k)
                deduped.append(k)
        return deduped[:count]


# ---------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------

def _parse_list(s: str) -> set[str]:
    """Split a comma-separated string into a set, stripping blanks."""
    return {x.strip() for x in s.split(",") if x.strip()}


def _collect_seeds(kb_root: Path, anchor_days: int) -> set[str]:
    """Return paper_keys that look recently-touched."""
    seeds: set[str] = set()
    seeds |= _seeds_from_git(kb_root, anchor_days)
    seeds |= _seeds_from_audit(kb_root, anchor_days)
    seeds |= _seeds_from_mtime(kb_root, anchor_days)
    return seeds


def _seeds_from_git(kb_root: Path, anchor_days: int) -> set[str]:
    """Paper mds changed in the last anchor_days per git log.

    Only counts commits that actually touched `papers/*.md`. If
    the KB root isn't a git repo, returns empty.
    """
    try:
        since = f"{anchor_days}.days.ago"
        # --name-only + --pretty=format: → just filenames.
        result = subprocess.run(
            [
                "git", "-C", str(kb_root), "log",
                f"--since={since}", "--name-only",
                "--pretty=format:",
                "--diff-filter=AM",   # added or modified
                "--", "papers/",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()

    seeds: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("papers/") or not line.endswith(".md"):
            continue
        # "papers/ABCD1234.md" → "ABCD1234"
        stem = line[len("papers/"):-len(".md")]
        if stem:
            seeds.add(stem)
    return seeds


def _seeds_from_audit(kb_root: Path, anchor_days: int) -> set[str]:
    """Papers mentioned in the kb-write audit log within anchor_days.

    audit.log is append-only JSONL at <kb_root>/.kb-mcp/audit.log
    (one line per write operation, written by kb_write.audit.
    record). Each line has a `ts` (RFC 3339) and `target` (kb-
    relative path). We filter lines by ts within window and by
    target starting with "papers/".

    Degrades to empty if the log file is missing or malformed —
    the signal is supplementary, not authoritative.
    """
    audit_path = kb_root / ".kb-mcp" / "audit.log"
    if not audit_path.is_file():
        return set()

    import json
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=anchor_days)

    seeds: set[str] = set()
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = entry.get("ts") or ""
                try:
                    if ts_raw.endswith("Z"):
                        ts_raw = ts_raw[:-1] + "+00:00"
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                target = entry.get("target") or ""
                if target.startswith("papers/") and target.endswith(".md"):
                    stem = target[len("papers/"):-len(".md")]
                    if stem:
                        seeds.add(stem)
    except OSError:
        return set()
    return seeds


def _seeds_from_mtime(kb_root: Path, anchor_days: int) -> set[str]:
    """Paper mds whose mtime is within anchor_days (filesystem signal).
    Catches edits that didn't go through git (rare but possible —
    e.g. a re-import)."""
    papers_dir = kb_root / "papers"
    if not papers_dir.is_dir():
        return set()
    cutoff = time.time() - (anchor_days * 86400)
    seeds: set[str] = set()
    for md in papers_dir.glob("*.md"):
        try:
            if md.stat().st_mtime >= cutoff:
                seeds.add(md.stem)
        except OSError:
            continue
    return seeds


def _expand_neighbours(
    kb_root: Path, seeds: set[str], edge_kinds: set[str],
) -> dict[str, int]:
    """Walk the kb_mcp links table one hop from seeds. Returns
    {neighbour_key: count_of_distinct_seeds_pointing_at_it}.

    Edge origins recognised:
      - kb_ref:    origin in ('frontmatter', 'wikilink', 'mdlink')
      - citation:  origin in ('cite', 'citation')
                   'cite' is a `@bibkey` reference written by the
                   user/agent in md body. 'citation' is a paper-to-
                   paper edge fetched from Semantic Scholar / OpenAlex
                   by kb_citations. Both represent "this paper
                   references that paper" in different directions of
                   authoring, so for "related-to-recent" purposes we
                   treat them as the same edge kind.
      - vec_similar: (future; currently no edge of this origin, so
                     passing this kind is a no-op)

    Degrades to empty dict if kb_mcp's projection DB doesn't exist
    or can't be opened.
    """
    if not seeds:
        return {}

    # Map our higher-level edge_kinds to DB origin values.
    origin_set: set[str] = set()
    if "kb_ref" in edge_kinds:
        origin_set |= {"frontmatter", "wikilink", "mdlink"}
    if "citation" in edge_kinds:
        origin_set |= {"cite", "citation"}
    if not origin_set:
        return {}

    db = kb_root / ".kb-mcp" / "index.sqlite"
    if not db.is_file():
        return {}

    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        try:
            conn.row_factory = sqlite3.Row
            origin_placeholders = ",".join("?" * len(origin_set))
            origin_list = list(origin_set)

            # Batch seeds to stay well below SQLite's `IN` variable
            # limit. Default limit varies across sqlite3 builds
            # (500 / 999 / 32766 depending on version), so we cap at
            # 400 per batch + origin_set size which is at most 5.
            # This keeps total params under 500 and works on every
            # supported SQLite.
            BATCH = 400
            out_rows: list = []
            in_rows: list = []
            seeds_list = list(seeds)
            for i in range(0, len(seeds_list), BATCH):
                batch = seeds_list[i:i + BATCH]
                batch_ph = ",".join("?" * len(batch))
                # Outbound: seed → neighbour
                out_rows.extend(conn.execute(
                    f"SELECT src_key, dst_key FROM links "
                    f"WHERE src_type='paper' "
                    f"  AND dst_type='paper' "
                    f"  AND src_key IN ({batch_ph}) "
                    f"  AND origin IN ({origin_placeholders})",
                    (*batch, *origin_list),
                ).fetchall())
                # Inbound: neighbour → seed
                in_rows.extend(conn.execute(
                    f"SELECT src_key, dst_key FROM links "
                    f"WHERE src_type='paper' "
                    f"  AND dst_type='paper' "
                    f"  AND dst_key IN ({batch_ph}) "
                    f"  AND origin IN ({origin_placeholders})",
                    (*batch, *origin_list),
                ).fetchall())
        finally:
            conn.close()
    except Exception:
        return {}

    # Count distinct seeds that touched each neighbour.
    # For outbound: neighbour is dst, seed is src.
    # For inbound: neighbour is src, seed is dst.
    neighbour_to_seeds: dict[str, set[str]] = {}
    for r in out_rows:
        seed = r["src_key"]; nbr = r["dst_key"]
        if nbr and nbr != seed:
            neighbour_to_seeds.setdefault(nbr, set()).add(seed)
    for r in in_rows:
        seed = r["dst_key"]; nbr = r["src_key"]
        if nbr and nbr != seed:
            neighbour_to_seeds.setdefault(nbr, set()).add(seed)

    return {nbr: len(seeds_set) for nbr, seeds_set in neighbour_to_seeds.items()}
