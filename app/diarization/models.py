from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start: float
    end: float
    speaker_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiarizationResult:
    provider_name: str
    model_name: str
    resolved_device: str
    turns: tuple[SpeakerTurn, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["turns"] = [turn.to_dict() for turn in self.turns]
        return result


@dataclass(frozen=True, slots=True)
class LabeledTranscriptSegment:
    start: float
    end: float
    text: str
    speaker_id: str | None
    speaker_name: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
