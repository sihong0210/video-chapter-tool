from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.diarization.models import DiarizationResult


class DiarizationError(RuntimeError):
    """Raised when speaker diarization cannot complete safely."""


class SpeakerDiarizer(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def analyze(
        self,
        audio_path: Path,
        *,
        speaker_count: int | None = None,
    ) -> DiarizationResult: ...
