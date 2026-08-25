from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.diarization.models import LabeledTranscriptSegment, SpeakerTurn
from app.transcription.transcriber import TranscriptSegment


def validate_speaker_turns(
    turns: Iterable[SpeakerTurn],
    *,
    duration_seconds: float | None = None,
) -> tuple[SpeakerTurn, ...]:
    result = tuple(sorted(turns, key=lambda item: (item.start, item.end)))
    previous_end = 0.0
    for turn in result:
        if turn.start < 0 or turn.end <= turn.start:
            raise ValueError("說話者時間區段包含無效 Timestamp。")
        if not turn.speaker_id.strip():
            raise ValueError("說話者 ID 不可為空。")
        if turn.start < previous_end - 0.001:
            raise ValueError("Exclusive 說話者時間區段不可互相重疊。")
        if duration_seconds is not None and turn.end > duration_seconds + 0.5:
            raise ValueError("說話者時間區段超出工作階段範圍。")
        previous_end = turn.end
    return result


def speaker_for_interval(
    start: float,
    end: float,
    turns: Iterable[SpeakerTurn],
) -> str | None:
    if end <= start:
        return None
    best_speaker: str | None = None
    best_overlap = 0.0
    midpoint = (start + end) / 2
    nearest_distance = float("inf")
    nearest_speaker: str | None = None
    for turn in turns:
        overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker_id
        distance = abs(midpoint - ((turn.start + turn.end) / 2))
        if distance < nearest_distance:
            nearest_distance = distance
            nearest_speaker = turn.speaker_id
    return best_speaker if best_overlap > 0 else nearest_speaker


def label_transcript_segments(
    segments: Iterable[TranscriptSegment],
    turns: Iterable[SpeakerTurn],
    *,
    labels: Mapping[str, str] | None = None,
) -> tuple[LabeledTranscriptSegment, ...]:
    turn_values = tuple(turns)
    label_values = labels or {}
    return tuple(
        LabeledTranscriptSegment(
            start=segment.start,
            end=segment.end,
            text=segment.text,
            speaker_id=(
                speaker_id := speaker_for_interval(
                    segment.start,
                    segment.end,
                    turn_values,
                )
            ),
            speaker_name=(label_values.get(speaker_id) if speaker_id else None),
        )
        for segment in segments
    )
