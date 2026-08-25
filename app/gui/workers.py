from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from app.infrastructure import redact_sensitive_text


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class RecordingSignals(QObject):
    """Thread-safe bridge from the recording workflow to the Qt interface."""

    status = Signal(str)
    capture_progress = Signal(object)
    stream_progress = Signal(object)
    segments = Signal(object)
    session_created = Signal(int)


class AIAnalysisSignals(QObject):
    """Thread-safe bridge from AI analysis callbacks to the Qt interface."""

    status = Signal(str)
    progress = Signal(object)


class MaintenanceSignals(QObject):
    """Thread-safe bridge for model downloads and session recovery."""

    status = Signal(str)
    progress = Signal(object)


class FunctionWorker(QRunnable):
    """Run one callable on Qt's shared thread pool without blocking the UI."""

    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:  # Qt worker boundary must report all failures.
            logging.getLogger("video_chapter_tool").error(
                "GUI background task failed: type=%s",
                type(exc).__name__,
            )
            message = redact_sensitive_text(f"{type(exc).__name__}: {exc}")
            self.signals.error.emit(message)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()
