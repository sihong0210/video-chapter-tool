from __future__ import annotations

import math
import sys
import threading
import time
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.audio.backend import AudioBackendUnavailable, load_pyaudio_backend
from app.audio.capture import CaptureError, DEFAULT_FRAMES_PER_BUFFER
from app.audio.chunking import AudioChunk, PcmChunkSpool
from app.audio.devices import LoopbackDevice, _select_loopback_info


MAX_STREAM_DURATION_SECONDS = 21_600.0
SILENCE_PADDING_GRACE_SECONDS = 0.25
DEFAULT_DEVICE_CHECK_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class StreamCaptureResult:
    spool_directory: str
    device: LoopbackDevice
    requested_duration_seconds: float
    captured_duration_seconds: float
    wall_time_seconds: float
    frames_captured: int
    source_frames_received: int
    silence_frames_padded: int
    chunks_produced: int
    stream_status_event_count: int
    cancelled_by_user: bool = False
    stopped_by_request: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["device"] = self.device.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class CaptureProgress:
    elapsed_seconds: float
    peak_level: float
    source_frames_received: int
    silence_frames_padded: int


def _validate_stream_request(
    duration_seconds: float,
    chunk_seconds: float,
    overlap_seconds: float,
) -> None:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("串流擷取秒數必須大於 0。")
    if duration_seconds > MAX_STREAM_DURATION_SECONDS:
        raise ValueError(
            f"單次串流驗證不可超過 {MAX_STREAM_DURATION_SECONDS:.0f} 秒。"
        )
    if not math.isfinite(chunk_seconds) or chunk_seconds <= 0:
        raise ValueError("Chunk 秒數必須大於 0。")
    if not math.isfinite(overlap_seconds) or overlap_seconds < 0:
        raise ValueError("Chunk 重疊秒數不可小於 0。")
    if overlap_seconds >= chunk_seconds:
        raise ValueError("Chunk 重疊秒數必須小於 Chunk 秒數。")


def _required_silence_frames(
    *,
    expected_timeline_frames: int,
    timeline_frames: int,
    grace_frames: int,
    target_frames: int,
) -> int:
    """Return wall-clock silence due without reacting to normal device jitter."""
    missing_frames = expected_timeline_frames - timeline_frames - grace_frames
    return max(0, min(missing_frames, target_frames - timeline_frames))


def _default_loopback_index(audio_manager: Any) -> int | None:
    try:
        default_info = audio_manager.get_default_wasapi_loopback()
    except (AttributeError, OSError):
        return None
    if not isinstance(default_info, dict) or "index" not in default_info:
        return None
    try:
        return int(default_info["index"])
    except (TypeError, ValueError):
        return None


def capture_loopback_to_chunks(
    *,
    duration_seconds: float,
    spool_directory: Path,
    on_chunk: Callable[[AudioChunk], None],
    device_index: int | None = None,
    chunk_seconds: float = 25.0,
    overlap_seconds: float = 2.0,
    frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
    on_stream_started: Callable[[], None] | None = None,
    on_capture_progress: Callable[[CaptureProgress], None] | None = None,
    stop_event: threading.Event | None = None,
) -> StreamCaptureResult:
    """Capture bounded loopback audio and durably emit overlapping WAV chunks."""

    _validate_stream_request(duration_seconds, chunk_seconds, overlap_seconds)
    if frames_per_buffer <= 0:
        raise ValueError("frames_per_buffer 必須大於 0。")

    spool_directory = spool_directory.expanduser().resolve()
    pyaudio = load_pyaudio_backend()
    capture_started = time.monotonic()

    try:
        with pyaudio.PyAudio() as audio_manager:
            device_info = _select_loopback_info(audio_manager, device_index)
            default_index = _default_loopback_index(audio_manager)
            device = LoopbackDevice.from_mapping(
                device_info,
                default_index=default_index,
            )
            sample_width = int(audio_manager.get_sample_size(pyaudio.paInt16))
            frame_width = sample_width * device.channels
            target_frames = max(1, round(duration_seconds * device.sample_rate))
            spool = PcmChunkSpool(
                spool_directory,
                sample_rate=device.sample_rate,
                channels=device.channels,
                sample_width=sample_width,
                chunk_seconds=chunk_seconds,
                overlap_seconds=overlap_seconds,
            )
            source_frames_received = 0
            silence_frames_padded = 0
            timeline_frames = 0
            chunks_produced = 0
            stream_status_event_count = 0
            target_reached = threading.Event()
            callback_errors: list[BaseException] = []
            cancelled_by_user = False
            stopped_by_request = False
            timeline_lock = threading.Lock()
            silence_block = b"\x00" * (frames_per_buffer * frame_width)
            grace_frames = max(
                frames_per_buffer,
                round(SILENCE_PADDING_GRACE_SECONDS * device.sample_rate),
            )
            last_progress_report = 0.0

            def peak_level(pcm_bytes: bytes) -> float:
                usable_length = len(pcm_bytes) - (len(pcm_bytes) % 2)
                if usable_length <= 0:
                    return 0.0
                values = array("h")
                values.frombytes(pcm_bytes[:usable_length])
                if sys.byteorder != "little":
                    values.byteswap()
                peak = max((abs(value) for value in values), default=0)
                return round(min(1.0, peak / 32768.0), 6)

            def report_capture_progress(level: float, *, force: bool = False) -> None:
                nonlocal last_progress_report
                if on_capture_progress is None:
                    return
                now = time.monotonic()
                if not force and now - last_progress_report < 0.1:
                    return
                last_progress_report = now
                with timeline_lock:
                    progress = CaptureProgress(
                        elapsed_seconds=round(
                            timeline_frames / device.sample_rate,
                            3,
                        ),
                        peak_level=level,
                        source_frames_received=source_frames_received,
                        silence_frames_padded=silence_frames_padded,
                    )
                try:
                    on_capture_progress(progress)
                except Exception:
                    pass

            def emit_chunks(chunks: tuple[AudioChunk, ...]) -> None:
                nonlocal chunks_produced
                for chunk in chunks:
                    on_chunk(chunk)
                    chunks_produced += 1

            def audio_callback(
                in_data: bytes,
                frame_count: int,
                _time_info: dict[str, float],
                status_flags: int,
            ) -> tuple[bytes, int]:
                nonlocal source_frames_received
                nonlocal stream_status_event_count
                nonlocal timeline_frames
                try:
                    if status_flags:
                        stream_status_event_count += 1
                    with timeline_lock:
                        remaining_frames = target_frames - timeline_frames
                        accepted_frames = min(
                            max(0, int(frame_count)),
                            max(0, remaining_frames),
                        )
                        valid_bytes = in_data[: accepted_frames * frame_width]
                        actual_frames = len(valid_bytes) // frame_width
                        if actual_frames:
                            valid_bytes = valid_bytes[: actual_frames * frame_width]
                            emit_chunks(spool.feed(valid_bytes))
                            source_frames_received += actual_frames
                            timeline_frames += actual_frames
                    report_capture_progress(peak_level(valid_bytes))
                    if timeline_frames >= target_frames:
                        target_reached.set()
                        return in_data, pyaudio.paComplete
                    return in_data, pyaudio.paContinue
                except BaseException as exc:
                    callback_errors.append(exc)
                    target_reached.set()
                    return in_data, pyaudio.paAbort

            with audio_manager.open(
                format=pyaudio.paInt16,
                channels=device.channels,
                rate=device.sample_rate,
                frames_per_buffer=frames_per_buffer,
                input=True,
                input_device_index=device.index,
                stream_callback=audio_callback,
            ):
                stream_started = time.monotonic()
                if on_stream_started is not None:
                    on_stream_started()
                deadline = stream_started + duration_seconds
                next_device_check = (
                    stream_started + DEFAULT_DEVICE_CHECK_INTERVAL_SECONDS
                )
                try:
                    while not target_reached.is_set():
                        now = time.monotonic()
                        if stop_event is not None and stop_event.is_set():
                            stopped_by_request = True
                            break
                        if device_index is None and now >= next_device_check:
                            current_default_index = _default_loopback_index(
                                audio_manager
                            )
                            if (
                                default_index is not None
                                and current_default_index is not None
                                and current_default_index != default_index
                            ):
                                raise CaptureError(
                                    "Windows 預設播放裝置已在擷取期間變更；"
                                    "已保存完成的逐字稿，請用新的播放裝置重新開始。"
                                )
                            next_device_check = (
                                now + DEFAULT_DEVICE_CHECK_INTERVAL_SECONDS
                            )
                        expected_timeline_frames = min(
                            target_frames,
                            round((now - stream_started) * device.sample_rate),
                        )
                        with timeline_lock:
                            padding_frames = _required_silence_frames(
                                expected_timeline_frames=expected_timeline_frames,
                                timeline_frames=timeline_frames,
                                grace_frames=grace_frames,
                                target_frames=target_frames,
                            )
                            while padding_frames > 0:
                                block_frames = min(
                                    frames_per_buffer,
                                    padding_frames,
                                )
                                emit_chunks(
                                    spool.feed(
                                        silence_block[: block_frames * frame_width]
                                    )
                                )
                                timeline_frames += block_frames
                                silence_frames_padded += block_frames
                                padding_frames -= block_frames
                            if timeline_frames >= target_frames:
                                target_reached.set()
                                break
                        if now >= deadline:
                            break
                        target_reached.wait(timeout=min(0.05, deadline - now))
                except KeyboardInterrupt:
                    cancelled_by_user = True

            if callback_errors:
                raise CaptureError(
                    "串流音訊 callback 失敗："
                    f"{type(callback_errors[0]).__name__}"
                ) from callback_errors[0]

            with timeline_lock:
                completion_target_frames = target_frames
                if cancelled_by_user or stopped_by_request:
                    completion_target_frames = min(
                        target_frames,
                        round(
                            (time.monotonic() - stream_started)
                            * device.sample_rate
                        ),
                    )
                remaining_padding = max(
                    0,
                    completion_target_frames - timeline_frames,
                )
                silence_frames_padded += remaining_padding
                while remaining_padding:
                    padding_frames = min(frames_per_buffer, remaining_padding)
                    emit_chunks(
                        spool.feed(silence_block[: padding_frames * frame_width])
                    )
                    timeline_frames += padding_frames
                    remaining_padding -= padding_frames
            emit_chunks(spool.finalize())
            report_capture_progress(0.0, force=True)

            captured_frames = timeline_frames
            return StreamCaptureResult(
                spool_directory=str(spool_directory),
                device=device,
                requested_duration_seconds=duration_seconds,
                captured_duration_seconds=round(
                    captured_frames / device.sample_rate,
                    6,
                ),
                wall_time_seconds=round(time.monotonic() - capture_started, 6),
                frames_captured=captured_frames,
                source_frames_received=source_frames_received,
                silence_frames_padded=silence_frames_padded,
                chunks_produced=chunks_produced,
                stream_status_event_count=stream_status_event_count,
                cancelled_by_user=cancelled_by_user,
                stopped_by_request=stopped_by_request,
            )
    except (AudioBackendUnavailable, CaptureError):
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CaptureError(f"WASAPI 串流擷取失敗：{exc}") from exc
