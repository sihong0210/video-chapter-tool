from __future__ import annotations

import math
import os
import shutil
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MINIMUM_FREE_DISK_RESERVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AudioChunk:
    index: int
    path: Path
    start_frame: int
    end_frame: int
    sample_rate: int
    channels: int
    sample_width: int

    @property
    def start_seconds(self) -> float:
        return self.start_frame / self.sample_rate

    @property
    def end_seconds(self) -> float:
        return self.end_frame / self.sample_rate

    @property
    def duration_seconds(self) -> float:
        return (self.end_frame - self.start_frame) / self.sample_rate

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["path"] = str(self.path)
        result["start_seconds"] = round(self.start_seconds, 6)
        result["end_seconds"] = round(self.end_seconds, 6)
        result["duration_seconds"] = round(self.duration_seconds, 6)
        return result


class PcmChunkSpool:
    """Write overlapping PCM chunks using source-frame counts as the clock."""

    def __init__(
        self,
        directory: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_width: int,
        chunk_seconds: float = 25.0,
        overlap_seconds: float = 2.0,
    ) -> None:
        if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
            raise ValueError("PCM 格式參數必須大於 0。")
        if not math.isfinite(chunk_seconds) or chunk_seconds <= 0:
            raise ValueError("Chunk 秒數必須大於 0。")
        if not math.isfinite(overlap_seconds) or overlap_seconds < 0:
            raise ValueError("Chunk 重疊秒數不可小於 0。")
        if overlap_seconds >= chunk_seconds:
            raise ValueError("Chunk 重疊秒數必須小於 Chunk 秒數。")

        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.frame_width = channels * sample_width
        self.chunk_frames = max(1, round(chunk_seconds * sample_rate))
        self.overlap_frames = max(0, round(overlap_seconds * sample_rate))
        self.stride_frames = self.chunk_frames - self.overlap_frames
        self._buffer = bytearray()
        self._buffer_start_frame = 0
        self._total_frames = 0
        self._last_emitted_end_frame = 0
        self._next_index = 0
        self._finalized = False

    @property
    def total_frames(self) -> int:
        return self._total_frames

    def feed(self, pcm_bytes: bytes) -> tuple[AudioChunk, ...]:
        if self._finalized:
            raise RuntimeError("Chunk spool 已結束，不能再加入音訊。")
        usable_length = len(pcm_bytes) - (len(pcm_bytes) % self.frame_width)
        if usable_length <= 0:
            return ()

        self._buffer.extend(pcm_bytes[:usable_length])
        self._total_frames += usable_length // self.frame_width
        emitted: list[AudioChunk] = []
        chunk_bytes = self.chunk_frames * self.frame_width
        stride_bytes = self.stride_frames * self.frame_width
        while len(self._buffer) >= chunk_bytes:
            data = bytes(self._buffer[:chunk_bytes])
            emitted.append(
                self._write_chunk(
                    data,
                    start_frame=self._buffer_start_frame,
                    frame_count=self.chunk_frames,
                )
            )
            del self._buffer[:stride_bytes]
            self._buffer_start_frame += self.stride_frames
        return tuple(emitted)

    def finalize(self) -> tuple[AudioChunk, ...]:
        if self._finalized:
            return ()
        self._finalized = True

        buffered_frames = len(self._buffer) // self.frame_width
        has_new_audio = self._total_frames > self._last_emitted_end_frame
        if buffered_frames <= 0 or not has_new_audio:
            return ()

        data = bytes(self._buffer[: buffered_frames * self.frame_width])
        return (
            self._write_chunk(
                data,
                start_frame=self._buffer_start_frame,
                frame_count=buffered_frames,
            ),
        )

    def _write_chunk(
        self,
        pcm_bytes: bytes,
        *,
        start_frame: int,
        frame_count: int,
    ) -> AudioChunk:
        index = self._next_index
        final_path = self.directory / f"chunk_{index:06d}.wav"
        temporary_path = final_path.with_suffix(".wav.part")
        free_bytes = shutil.disk_usage(self.directory).free
        required_bytes = len(pcm_bytes) + MINIMUM_FREE_DISK_RESERVE_BYTES
        if free_bytes < required_bytes:
            raise OSError(
                "PendingAudio 磁碟空間不足；"
                f"至少需保留 {required_bytes / 1024**2:.1f} MiB。"
            )
        try:
            with wave.open(str(temporary_path), "wb") as wav_file:
                wav_file.setnchannels(self.channels)
                wav_file.setsampwidth(self.sample_width)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(pcm_bytes)
            os.replace(temporary_path, final_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise

        end_frame = start_frame + frame_count
        chunk = AudioChunk(
            index=index,
            path=final_path,
            start_frame=start_frame,
            end_frame=end_frame,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
        )
        self._next_index += 1
        self._last_emitted_end_frame = max(
            self._last_emitted_end_frame,
            end_frame,
        )
        return chunk
