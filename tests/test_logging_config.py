from __future__ import annotations

import logging
import tempfile
import unittest
from logging.handlers import RotatingFileHandler

from app.config import AppPaths
from app.infrastructure.logging_config import (
    SensitiveDataFilter,
    close_logging,
    configure_logging,
    redact_sensitive_text,
)


class LoggingConfigTests(unittest.TestCase):
    def test_redacts_common_api_key_and_token_formats(self) -> None:
        original = (
            "api_key=secret-value token:another-secret "
            "Authorization Bearer abc.def-123 sk-example0123456789"
        )

        redacted = redact_sensitive_text(original)

        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("another-secret", redacted)
        self.assertNotIn("abc.def-123", redacted)
        self.assertNotIn("sk-example0123456789", redacted)

    def test_logging_filter_handles_parameterized_messages(self) -> None:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="token=%s",
            args=("sensitive-token",),
            exc_info=None,
        )

        self.assertTrue(SensitiveDataFilter().filter(record))
        self.assertEqual(record.getMessage(), "token=[REDACTED]")

    def test_configure_logging_writes_redacted_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            logger = configure_logging(paths, console=False)
            try:
                logger.warning("api_key=%s", "do-not-store-this")
                for handler in logger.handlers:
                    handler.flush()
            finally:
                close_logging(logger)

            contents = paths.application_log_file.read_text(encoding="utf-8")
            self.assertIn("api_key=[REDACTED]", contents)
            self.assertNotIn("do-not-store-this", contents)

    def test_file_log_has_bounded_rotation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            logger = configure_logging(
                AppPaths.from_root(temporary_directory),
                console=False,
            )
            try:
                handler = next(
                    item
                    for item in logger.handlers
                    if isinstance(item, RotatingFileHandler)
                )
                self.assertEqual(handler.maxBytes, 5 * 1024 * 1024)
                self.assertEqual(handler.backupCount, 5)
            finally:
                close_logging(logger)


if __name__ == "__main__":
    unittest.main()
