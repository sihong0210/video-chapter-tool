from __future__ import annotations

import ctypes
import os
from collections.abc import Callable


ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000


def recording_execution_flags(*, keep_display_on: bool) -> int:
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if keep_display_on:
        flags |= ES_DISPLAY_REQUIRED
    return flags


class RecordingPowerRequest:
    """Keep Windows awake only for the lifetime of an active recording."""

    def __init__(
        self,
        *,
        keep_display_on: bool,
        setter: Callable[[int], int] | None = None,
    ) -> None:
        self.keep_display_on = keep_display_on
        self._setter = setter
        self.active = False

    def acquire(self) -> bool:
        if os.name != "nt" and self._setter is None:
            return False
        try:
            setter = self._setter
            if setter is None:
                setter = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
            result = int(setter(recording_execution_flags(
                keep_display_on=self.keep_display_on
            )))
        except (AttributeError, OSError, TypeError, ValueError):
            self.active = False
            return False
        self.active = result != 0
        return self.active

    def release(self) -> None:
        if not self.active:
            return
        try:
            setter = self._setter
            if setter is None:
                setter = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
            setter(ES_CONTINUOUS)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        self.active = False

    def __enter__(self) -> "RecordingPowerRequest":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
