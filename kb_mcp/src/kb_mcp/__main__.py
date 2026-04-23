"""Entry point for `python -m kb_mcp`."""
import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
