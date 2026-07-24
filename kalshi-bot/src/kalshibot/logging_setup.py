"""Structured JSON logging to rotating files.

Rotation is bounded so logs cannot fill the disk over months of running.
A redaction filter strips anything that looks like a credential so keys are
never written even at DEBUG level.
"""
from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Patterns that must never appear in logs. If a message or field value matches,
# it is replaced. This is defence-in-depth: code should never pass a secret to
# the logger in the first place.
_REDACT_PATTERNS = [
    re.compile(r"-----BEGIN [^-]+PRIVATE KEY-----.*?-----END [^-]+PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(KALSHI-ACCESS-SIGNATURE\s*[:=]\s*)\S+", re.IGNORECASE),
]


def _redact(text: str) -> str:
    for pat in _REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = _redact(self.formatException(record.exc_info))
        # Structured extras attached via logger.info(msg, extra={"fields": {...}})
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload["fields"] = {k: _redact(str(v)) for k, v in fields.items()}
        return json.dumps(payload)


def setup_logging(log_dir: str, level: str = "INFO", max_bytes: int = 10_485_760,
                  backup_count: int = 20) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("kalshibot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    file_handler = RotatingFileHandler(
        Path(log_dir) / "kalshibot.jsonl", maxBytes=max_bytes, backupCount=backup_count
    )
    file_handler.setFormatter(_JsonFormatter())
    logger.addHandler(file_handler)

    # Also echo to stderr for interactive runs.
    stream = logging.StreamHandler()
    stream.setFormatter(_JsonFormatter())
    logger.addHandler(stream)

    logger.propagate = False
    return logger


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Helper so callers can attach structured fields concisely."""
    logger.log(level, msg, extra={"fields": fields} if fields else None)
