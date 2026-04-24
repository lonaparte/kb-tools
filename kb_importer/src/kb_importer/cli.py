"""kb-importer main CLI entry point."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .commands import (
    import_cmd,
    list_cmd,
    orphans_cmd,
    preflight_cmd,
    status_cmd,
    summary_cmd,
    sync_cmd,
)
from .config import ConfigError, load_config
from .logging_util import setup_logging

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kb-importer",
        description=(
            "Translate a Zotero library into KB markdown files. "
            "Reads metadata from the Zotero web API by default; finds "
            "PDFs in the configured storage directory."
        ),
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s v{__version__}")
    parser.add_argument(
        "--zotero-storage", type=Path,
        dest="zotero_storage_dir",
        help=(
            "Path to the directory containing per-item Zotero storage "
            "subdirs (typically ~/Zotero/storage). Overrides config/env."
        ),
    )
    parser.add_argument(
        "--zotero-source",
        choices=["live", "web"],
        dest="zotero_source_mode",
        help=(
            "Metadata source: 'web' (cloud API, needs library_id + "
            "API key; default since 0.28.0) or 'live' (local Zotero "
            "at localhost:23119, requires Zotero running locally)."
        ),
    )
    parser.add_argument(
        "--zotero-library-id",
        dest="zotero_library_id",
        help=(
            "Zotero userID (for --zotero-source=web). Find at "
            "https://www.zotero.org/settings/keys"
        ),
    )
    parser.add_argument(
        "--zotero-mirror", type=Path,
        dest="_legacy_zotero_mirror",
        help=argparse.SUPPRESS,  # deprecated, accepted for back-compat
    )
    parser.add_argument(
        "--kb-root", type=Path,
        help="Path to KB repository root (overrides config/env).",
    )
    parser.add_argument(
        "--config", type=Path,
        help="Path to config.yaml (overrides default location).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't write any files or move PDFs; just preview.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    status_cmd.add_parser(subparsers)
    list_cmd.add_parser(subparsers)
    import_cmd.add_parser(subparsers)
    sync_cmd.add_parser(subparsers)
    orphans_cmd.add_orphans_parser(subparsers)
    # 0.29.1: `unarchive` subcommand removed with the _archived/
    # feature removal.
    summary_cmd.add_set_summary_parser(subparsers)
    summary_cmd.add_import_summaries_parser(subparsers)
    summary_cmd.add_show_template_parser(subparsers)
    preflight_cmd.add_parser(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Back-compat: if user passed the deprecated --zotero-mirror, route
    # it through as if it came from config (config.load_config handles
    # the translation and warning).
    legacy_mirror = getattr(args, "_legacy_zotero_mirror", None)
    if legacy_mirror and not args.zotero_storage_dir:
        import os
        os.environ["KB_ZOTERO_MIRROR"] = str(legacy_mirror)

    # preflight is a diagnostic command that pings the LLM provider;
    # it shouldn't require a fully-configured Zotero workspace.
    # Dispatch directly with a minimal cfg shim — this lets a user
    # run `kb-importer preflight --fulltext-provider openrouter`
    # before they've set up library_id / zotero_storage.
    if args.command == "preflight":
        # preflight_cmd never touches cfg.kb_root / cfg.zotero_*;
        # it only uses the command's own args. Passing None is
        # acceptable and keeps type-checkers happy at the call site.
        return args.func(args, None)

    # Load config; CLI args override env vars which override file.
    try:
        cfg = load_config(
            config_path=args.config,
            zotero_storage_dir=args.zotero_storage_dir,
            kb_root=args.kb_root,
            zotero_source_mode=args.zotero_source_mode,
            zotero_library_id=args.zotero_library_id,
        )
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    # Adjust log level from flags.
    level = cfg.log_level
    if args.verbose:
        level = "debug"
    elif args.quiet:
        level = "warning"
    setup_logging(level=level, log_file=cfg.log_file)

    log.debug(
        "Resolved config: source=%s, storage=%s, kb=%s",
        cfg.zotero_source_mode, cfg.zotero_storage_dir, cfg.kb_root,
    )
    if cfg.zotero_source_mode == "web":
        log.debug(
            "Web mode: library_id=%s, library_type=%s, api_key_env=%s",
            cfg.zotero_library_id, cfg.zotero_library_type,
            cfg.zotero_api_key_env,
        )

    # Ensure KB directories exist.
    cfg.papers_dir.mkdir(parents=True, exist_ok=True)
    cfg.notes_dir.mkdir(parents=True, exist_ok=True)

    # Dispatch.
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        log.exception("Unhandled error")
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
