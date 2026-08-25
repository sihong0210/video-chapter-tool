from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from app.config.paths import AppPaths


LOGGER_NAME = "video_chapter_tool"

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token)"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered_message = record.getMessage()
        except Exception:
            rendered_message = str(record.msg)
        record.msg = redact_sensitive_text(rendered_message)
        record.args = ()
        return True


def configure_logging(
    paths: AppPaths,
    level: str = "INFO",
    *,
    console: bool = True,
) -> logging.Logger:
    paths.ensure()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    sensitive_data_filter = SensitiveDataFilter()

    file_handler = RotatingFileHandler(
        paths.application_log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(sensitive_data_filter)
    logger.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(sensitive_data_filter)
        logger.addHandler(console_handler)

    return logger


def close_logging(logger: logging.Logger) -> None:
    """Flush and close application-owned handlers, especially on Windows."""

    for handler in logger.handlers[:]:
        try:
            handler.flush()
        finally:
            handler.close()
            logger.removeHandler(handler)
