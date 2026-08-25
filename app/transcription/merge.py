from __future__ import annotations

from dataclasses import replace

from app.transcription.transcriber import TranscriptSegment, TranscriptWord


def _clip_words(
    words: tuple[TranscriptWord, ...],
    *,
    start: float,
    end: float,
) -> tuple[TranscriptWord, ...]:
    clipped: list[TranscriptWord] = []
    for word in words:
        word_start = max(start, word.start)
        word_end = min(end, word.end)
        if word_end <= word_start:
            continue
        clipped.append(
            replace(
                word,
                start=round(word_start, 3),
                end=round(word_end, 3),
            )
        )
    return tuple(clipped)


def _normalized_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _trim_repeated_prefix(previous: str, current: str) -> str:
    previous = previous.strip()
    current = current.strip()
    normalized_previous = _normalized_text(previous)
    normalized_current_characters: list[str] = []
    current_end_positions: list[int] = []
    for index, character in enumerate(current):
        if not character.isalnum():
            continue
        for folded_character in character.casefold():
            normalized_current_characters.append(folded_character)
            current_end_positions.append(index + 1)
    normalized_current = "".join(normalized_current_characters)
    maximum = min(len(normalized_previous), len(normalized_current))
    for length in range(maximum, 3, -1):
        if normalized_previous[-length:] == normalized_current[:length]:
            original_end = current_end_positions[length - 1]
            return current[original_end:].lstrip(" ，,。.!！?？、：:；;")
    return current


def _trim_through_repeated_overlap(previous: str, current: str) -> str:
    normalized_previous = _normalized_text(previous)
    normalized_current_characters: list[str] = []
    current_end_positions: list[int] = []
    for index, character in enumerate(current):
        if not character.isalnum():
            continue
        for folded_character in character.casefold():
            normalized_current_characters.append(folded_character)
            current_end_positions.append(index + 1)
    normalized_current = "".join(normalized_current_characters)
    maximum = min(len(normalized_previous), len(normalized_current))
    for length in range(maximum, 3, -1):
        repeated = normalized_previous[-length:]
        position = normalized_current.find(repeated)
        if position >= 0:
            original_end = current_end_positions[position + length - 1]
            return current[original_end:].lstrip(" ，,。.!！?？、：:；;")
    return current


class TranscriptMerger:
    """Merge overlapping chunk transcripts onto one sample-based timeline."""

    def __init__(self, *, overlap_seconds: float) -> None:
        if overlap_seconds < 0:
            raise ValueError("重疊秒數不可小於 0。")
        self.overlap_seconds = overlap_seconds
        self._segments: list[TranscriptSegment] = []

    @property
    def segments(self) -> tuple[TranscriptSegment, ...]:
        return tuple(self._segments)

    def seed(self, segments: tuple[TranscriptSegment, ...]) -> None:
        if self._segments:
            raise RuntimeError("已有逐字稿時不能再次初始化合併器。")
        previous_end = 0.0
        for segment in segments:
            if segment.start < 0 or segment.end <= segment.start:
                raise ValueError("既有逐字稿包含無效 Timestamp。")
            if segment.start < previous_end:
                raise ValueError("既有逐字稿時間軸不是單調遞增。")
            self._segments.append(segment)
            previous_end = segment.end

    def add_chunk(
        self,
        chunk_start_seconds: float,
        segments: tuple[TranscriptSegment, ...],
        *,
        chunk_end_seconds: float | None = None,
    ) -> tuple[TranscriptSegment, ...]:
        existing_before_chunk = tuple(self._segments)
        overlap_cutoff = (
            chunk_start_seconds + self.overlap_seconds
            if existing_before_chunk
            else chunk_start_seconds
        )
        previous_overlap_text = " ".join(
            segment.text
            for segment in existing_before_chunk[-8:]
            if segment.end >= chunk_start_seconds - 0.5
        )
        added: list[TranscriptSegment] = []
        for local_segment in segments:
            absolute_start = max(
                chunk_start_seconds,
                chunk_start_seconds + local_segment.start,
            )
            absolute_end = chunk_start_seconds + local_segment.end
            if chunk_end_seconds is not None:
                absolute_end = min(absolute_end, chunk_end_seconds)
            candidate = replace(
                local_segment,
                start=round(absolute_start, 3),
                end=round(absolute_end, 3),
                text=local_segment.text.strip(),
                words=tuple(
                    replace(
                        word,
                        start=round(chunk_start_seconds + word.start, 3),
                        end=round(chunk_start_seconds + word.end, 3),
                    )
                    for word in local_segment.words
                ),
            )
            candidate = replace(
                candidate,
                words=_clip_words(
                    candidate.words,
                    start=candidate.start,
                    end=candidate.end,
                ),
            )
            if not candidate.text or candidate.end <= candidate.start:
                continue

            if existing_before_chunk and candidate.start < overlap_cutoff:
                if candidate.end <= overlap_cutoff:
                    continue
                trimmed = _trim_through_repeated_overlap(
                    previous_overlap_text,
                    candidate.text,
                )
                if not trimmed:
                    continue
                candidate = replace(
                    candidate,
                    start=round(overlap_cutoff, 3),
                    text=trimmed,
                    words=_clip_words(
                        candidate.words,
                        start=overlap_cutoff,
                        end=candidate.end,
                    ),
                )

            duplicate = False
            normalized_candidate = _normalized_text(candidate.text)
            for existing in reversed(self._segments[-6:]):
                if candidate.start > existing.end + self.overlap_seconds + 1.0:
                    break
                same_text = _normalized_text(existing.text) == normalized_candidate
                overlapping_time = (
                    candidate.start <= existing.end + 0.5
                    and candidate.end >= existing.start - 0.5
                )
                if same_text and overlapping_time:
                    duplicate = True
                    break
            if duplicate:
                continue

            if self._segments:
                previous = self._segments[-1]
                if candidate.start <= previous.end + self.overlap_seconds:
                    trimmed = _trim_repeated_prefix(previous.text, candidate.text)
                    if not trimmed:
                        continue
                    if trimmed != candidate.text:
                        candidate = replace(
                            candidate,
                            start=round(max(candidate.start, previous.end), 3),
                            text=trimmed,
                            words=_clip_words(
                                candidate.words,
                                start=max(candidate.start, previous.end),
                                end=candidate.end,
                            ),
                        )

            if self._segments and candidate.start < self._segments[-1].end:
                candidate = replace(
                    candidate,
                    start=round(self._segments[-1].end, 3),
                    words=_clip_words(
                        candidate.words,
                        start=self._segments[-1].end,
                        end=candidate.end,
                    ),
                )
            if candidate.end <= candidate.start:
                continue
            self._segments.append(candidate)
            added.append(candidate)
        return tuple(added)
