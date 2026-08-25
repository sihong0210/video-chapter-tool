from __future__ import annotations

import os
import wave
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import numpy as np


class ChunkRecord(Protocol):
    path: str
    start_time: float
    end_time: float


class DiarizationAudioError(RuntimeError):
    """Raised when retained Chunk audio cannot form one session timeline."""


def assemble_session_wav(
    chunks: Iterable[ChunkRecord],
    output_path: Path,
    *,
    target_sample_rate: int | None = None,
) -> Path:
    """Rebuild a session WAV while removing overlap duplicated by Chunking."""
    records = tuple(sorted(chunks, key=lambda item: float(item.start_time)))
    if not records:
        raise DiarizationAudioError("工作階段沒有可用的音訊 Chunk。")
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".part")
    current_frame = 0
    output_frame = 0
    expected_format: tuple[int, int, int] | None = None
    try:
        with wave.open(str(temporary_path), "wb") as output:
            for record in records:
                source_path = Path(record.path).expanduser().resolve()
                if not source_path.is_file():
                    raise DiarizationAudioError(
                        f"說話者分析所需 Chunk 不存在：{source_path}"
                    )
                try:
                    with wave.open(str(source_path), "rb") as source:
                        audio_format = (
                            source.getframerate(),
                            source.getnchannels(),
                            source.getsampwidth(),
                        )
                        if expected_format is None:
                            expected_format = audio_format
                            if target_sample_rate is None:
                                output.setframerate(audio_format[0])
                                output.setnchannels(audio_format[1])
                                output.setsampwidth(audio_format[2])
                            else:
                                if target_sample_rate <= 0:
                                    raise ValueError("目標取樣率必須大於 0。")
                                if audio_format[2] != 2:
                                    raise DiarizationAudioError(
                                        "說話者分析目前只支援 16-bit PCM Chunk。"
                                    )
                                output.setframerate(target_sample_rate)
                                output.setnchannels(1)
                                output.setsampwidth(2)
                        elif audio_format != expected_format:
                            raise DiarizationAudioError(
                                "工作階段 Chunk 的 PCM 格式不一致，無法安全重建。"
                            )
                        sample_rate, channels, sample_width = audio_format
                        start_frame = round(float(record.start_time) * sample_rate)
                        end_frame = round(float(record.end_time) * sample_rate)
                        if start_frame > current_frame:
                            gap_frames = start_frame - current_frame
                            if target_sample_rate is None:
                                output.writeframes(
                                    b"\x00" * gap_frames * channels * sample_width
                                )
                            else:
                                target_gap_end = round(
                                    start_frame / sample_rate * target_sample_rate
                                )
                                output.writeframes(
                                    b"\x00" * max(0, target_gap_end - output_frame) * 2
                                )
                                output_frame = target_gap_end
                            current_frame = start_frame
                        skip_frames = max(0, current_frame - start_frame)
                        if skip_frames:
                            source.setpos(min(skip_frames, source.getnframes()))
                        available_frames = max(
                            0,
                            min(source.getnframes(), end_frame - start_frame)
                            - skip_frames,
                        )
                        if available_frames:
                            pcm = source.readframes(available_frames)
                            if target_sample_rate is None:
                                output.writeframes(pcm)
                                output_frame += available_frames
                            else:
                                samples = np.frombuffer(pcm, dtype="<i2")
                                if channels > 1:
                                    samples = samples.reshape(-1, channels).mean(axis=1)
                                else:
                                    samples = samples.astype(np.float64)
                                target_end = round(
                                    (current_frame + available_frames)
                                    / sample_rate
                                    * target_sample_rate
                                )
                                target_count = max(0, target_end - output_frame)
                                if target_count and len(samples):
                                    positions = np.linspace(
                                        0,
                                        len(samples) - 1,
                                        num=target_count,
                                    )
                                    converted = np.interp(
                                        positions,
                                        np.arange(len(samples)),
                                        samples,
                                    )
                                    converted = np.clip(
                                        np.rint(converted),
                                        -32768,
                                        32767,
                                    ).astype("<i2")
                                    output.writeframes(converted.tobytes())
                                output_frame = target_end
                            current_frame += available_frames
                except (OSError, wave.Error) as exc:
                    raise DiarizationAudioError(
                        f"無法讀取說話者分析 Chunk：{source_path}；{exc}"
                    ) from exc
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path
