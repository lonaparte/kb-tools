"""Simple logging setup.

Human-readable to stderr, optional JSONL file for later analysis.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path


class JsonlFormatter(logging.Formatter):
    """Emit one JSON object per line; includes structured extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        # Capture anything attached via logger.info("...", extra={...}).
        for k, v in record.__dict__.items():
            if k.startswith("_"):
                continue
            if k in (
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "levelname", "levelno", "lineno",
                "module", "msecs", "message", "msg", "name", "pathname",
                "process", "processName", "relativeCreated", "stack_info",
                "thread", "threadName", "taskName",
            ):
                continue
            try:
                json.dumps(v)  # ensure serializable
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "info", log_file: Path | None = None) -> None:
    """Configure root logger. Idempotent-ish: clears existing handlers."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    # Human-readable stderr handler.
    stderr = logging.StreamHandler()
    stderr.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(stderr)

    # Optional JSONL file handler.
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setFormatter(JsonlFormatter())
        root.addHandler(fh)
