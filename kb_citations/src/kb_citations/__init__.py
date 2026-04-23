"""kb_citations — paper-to-paper citation edges for ee-kb.

Two providers: Semantic Scholar, OpenAlex. Unified interface:
pass `CitationsContext` + a provider instance to `fetch_all`,
cache lands under `<kb_root>/.kb-mcp/citations/`. Then run
`link` to push edges into kb-mcp's `links` table (origin='citation').

See README.md and the `kb-citations --help` CLI for usage.
"""
__version__ = "0.27.8"

from .config import CitationsContext
from .provider import Reference, normalize_doi

__all__ = ["CitationsContext", "Reference", "normalize_doi"]
