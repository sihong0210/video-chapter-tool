from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.transcription.engine import RuntimeInfo, WhisperEngine


class TranscriptionError(RuntimeError):
    """Raised when an audio file cannot be transcribed."""


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    start: float
    end: float
    text: str
    probability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    average_log_probability: float
    no_speech_probability: float
    words: tuple[TranscriptWord, ...] = ()
    speaker_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    source_path: str
    language: str | None
    language_probability: float | None
    audio_duration_seconds: float
    duration_after_vad_seconds: float
    transcription_seconds: float
    real_time_factor: float | None
    runtime: RuntimeInfo
    segments: tuple[TranscriptSegment, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["runtime"] = self.runtime.to_dict()
        result["segments"] = [segment.to_dict() for segment in self.segments]
        return result


class BasicTranscriber:
    def __init__(self, engine: WhisperEngine) -> None:
        self.engine = engine

    def transcribe_file(
        self,
        source_path: Path,
        *,
        language: str | None,
        beam_size: int = 5,
        vad_filter: bool = True,
        word_timestamps: bool = False,
    ) -> TranscriptResult:
        source_path = source_path.expanduser().resolve()
        if not source_path.is_file():
            raise TranscriptionError(f"找不到音訊檔案：{source_path}")
        if beam_size <= 0:
            raise ValueError("beam_size 必須大於 0。")

        started = time.monotonic()
        try:
            raw_segments, info = self.engine.model.transcribe(
                str(source_path),
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                word_timestamps=word_timestamps,
                condition_on_previous_text=True,
            )
            segments = tuple(
                TranscriptSegment(
                    start=round(float(segment.start), 3),
                    end=round(float(segment.end), 3),
                    text=str(segment.text).strip(),
                    average_log_probability=round(float(segment.avg_logprob), 6),
                    no_speech_probability=round(float(segment.no_speech_prob), 6),
                    words=tuple(
                        TranscriptWord(
                            start=round(float(word.start), 3),
                            end=round(float(word.end), 3),
                            text=str(word.word),
                            probability=(
                                round(float(word.probability), 6)
                                if getattr(word, "probability", None) is not None
                                else None
                            ),
                        )
                        for word in (getattr(segment, "words", None) or ())
                        if str(getattr(word, "word", ""))
                        and float(word.end) > float(word.start)
                    ),
                )
                for segment in raw_segments
                if str(segment.text).strip()
            )
        except Exception as exc:
            raise TranscriptionError(
                f"音訊轉錄失敗：{type(exc).__name__}: {exc}"
            ) from exc

        elapsed = time.monotonic() - started
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        duration_after_vad = float(
            getattr(info, "duration_after_vad", duration) or 0.0
        )
        real_time_factor = round(elapsed / duration, 6) if duration > 0 else None
        language_value = getattr(info, "language", None)
        language_probability = getattr(info, "language_probability", None)
        return TranscriptResult(
            source_path=str(source_path),
            language=str(language_value) if language_value else None,
            language_probability=(
                round(float(language_probability), 6)
                if language_probability is not None
                else None
            ),
            audio_duration_seconds=round(duration, 6),
            duration_after_vad_seconds=round(duration_after_vad, 6),
            transcription_seconds=round(elapsed, 6),
            real_time_factor=real_time_factor,
            runtime=self.engine.runtime,
            segments=segments,
        )
