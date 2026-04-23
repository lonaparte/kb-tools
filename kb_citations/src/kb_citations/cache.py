"""Local cache for fetched citation data.

Purpose: a provider call for 1200 papers takes ~20 minutes. If the
run crashes or you want to re-link without re-fetching, the cache
saves you. It's also what enables incremental fetches.

Layout:
    <kb_root>/.kb-mcp/citations/
    ├── by-paper/
    │   ├── ABCD1234.json      # { provider, fetched_at, references: [...], citations: [...] }
    │   ├── EFGH5678.json
    │   └── ...
    └── index.json              # per-provider cursor: last fetched when

Each paper file is independent so we can update one without
touching others. index.json is a lightweight summary for the CLI's
`status` command.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .provider import Reference


log = logging.getLogger(__name__)


CACHE_SUBPATH = ".kb-mcp/citations"


class CitationCache:
    """File-backed cache of per-paper fetch results.

    One JSON file per local paper key. Keys never collide because
    they're Zotero keys (globally unique within a KB).
    """

    def __init__(self, kb_root: Path):
        self.root = Path(kb_root) / CACHE_SUBPATH
        self.by_paper_dir = self.root / "by-paper"
        self.index_path = self.root / "index.json"

    def ensure_dirs(self) -> None:
        self.by_paper_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, paper_key: str) -> Path:
        return self.by_paper_dir / f"{paper_key}.json"

    # ------------------------------------------------------------
    # Per-paper read/write
    # ------------------------------------------------------------

    def load(self, paper_key: str) -> dict | None:
        """Return cached payload for `paper_key`, or None."""
        p = self.path_for(paper_key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("cache read failed for %s: %s", paper_key, e)
            return None

    def save(
        self,
        paper_key: str,
        *,
        provider: str,
        references: Iterable[Reference] = (),
        citations: Iterable[Reference] = (),
        doi: str | None = None,
    ) -> None:
        """Write cache for a paper. Atomic (temp + rename)."""
        self.ensure_dirs()
        payload = {
            "paper_key": paper_key,
            "doi": doi,
            "provider": provider,
            "fetched_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "references": [asdict(r) for r in references],
            "citations": [asdict(r) for r in citations],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.path_for(paper_key).with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path_for(paper_key))

    def is_fresh(self, paper_key: str, *, max_age_days: int | None = None) -> bool:
        """True if cache exists and (if max_age_days given) is newer
        than that."""
        data = self.load(paper_key)
        if not data:
            return False
        if max_age_days is None:
            return True
        try:
            fetched = datetime.fromisoformat(
                data["fetched_at"].replace("Z", "+00:00")
            )
            age = datetime.now(timezone.utc) - fetched
            return age.days < max_age_days
        except Exception:
            return False

    # ------------------------------------------------------------
    # Index / summary
    # ------------------------------------------------------------

    def all_keys(self) -> list[str]:
        """Return all paper keys that have cached data."""
        if not self.by_paper_dir.exists():
            return []
        return sorted(p.stem for p in self.by_paper_dir.glob("*.json"))

    def summary(self) -> dict:
        """Aggregate stats for `kb-citations status`."""
        keys = self.all_keys()
        total_refs = 0
        total_cites = 0
        providers: dict[str, int] = {}
        for k in keys:
            data = self.load(k)
            if not data:
                continue
            total_refs += len(data.get("references") or [])
            total_cites += len(data.get("citations") or [])
            prov = data.get("provider", "unknown")
            providers[prov] = providers.get(prov, 0) + 1
        return {
            "cached_papers": len(keys),
            "total_references_fetched": total_refs,
            "total_citations_fetched": total_cites,
            "by_provider": providers,
        }
