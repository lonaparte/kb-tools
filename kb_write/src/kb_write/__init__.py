"""kb_write — client-agnostic write layer for the ee-kb knowledge base.

Two client entry points:
  - CLI: `kb-write` (see kb_write.cli)
  - Python: `from kb_write.ops import thought, topic, preference, init`

Both funnel through the same validators (kb_write.rules) and the
same atomic-write + mtime-guard + git-commit pipeline.

See AGENT-WRITE-RULES.md (shipped at the package root) for the
normative write rules.
"""
__version__ = "0.27.6"

from .config import WriteContext
from .rules import RuleViolation
from .atomic import WriteConflictError, WriteExistsError
from .zones import ZoneError
from .paths import PathError, NodeAddress, parse_target

__all__ = [
    "WriteContext",
    "RuleViolation",
    "WriteConflictError",
    "WriteExistsError",
    "ZoneError",
    "PathError",
    "NodeAddress",
    "parse_target",
]
