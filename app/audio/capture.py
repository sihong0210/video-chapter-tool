from __future__ import annotations

import math
import threading
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.audio.backend import AudioBackendUnavailable, load_pyaudio_backend
from app.audio.devices import LoopbackDevice, _select_loopback_info


DEFAULT_FRAMES_PER_BUFFER = 1024
MAX_PROTOTYPE_DURATION_SECONDS = 300.0


class CaptureError(RuntimeError):
    """Raised when a bounded loopback capture cannot complete."""


@dataclass(frozen=True, slots=True)
class CaptureResult:
    output_path: str
    device: LoopbackDevice
    requested_duration_seconds: float
    captured_duration_seconds: float
    wall_time_seconds: float
    frames_captured: int
    source_frames_received: int
    silence_frames_padded: int
    bytes_written: int
    stream_status_event_count: int
    peak_level: float
    rms_level: float

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["device"] = self.device.to_dict()
        return result


class _PcmS16LeStatistics:
    def __init__(self) -> None:
        self.sample_count = 0
        self.square_sum = 0
        self.absolute_peak = 0

    def add(self, pcm_bytes: bytes) -> None:
        usable_length = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable_length <= 0:
            return
        samples = array("h")
        samples.frombytes(pcm_bytes[:usable_length])

        local_peak = 0
        local_square_sum = 0
        for sample in samples:
            absolute_sample = abs(sample)
            if absolute_sample > local_peak:
                local_peak = absolute_sample
            local_square_sum += sample * sample

        self.sample_count += len(samples)
        self.square_sum += local_square_sum
        self.absolute_peak = max(self.absolute_peak, local_peak)

    @property
    def peak_level(self) -> float:
        return round(min(1.0, self.absolute_peak / 32768.0), 6)

    @property
    def rms_level(self) -> float:
        if self.sample_count == 0:
            return 0.0
        rms = math.sqrt(self.square_sum / self.sample_count) / 32768.0
        return round(min(1.0, rms), 6)


def _validate_capture_request(duration_seconds: float, output_path: Path) -> None:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("擷取秒數必須大於 0。")
    if duration_seconds > MAX_PROTOTYPE_DURATION_SECONDS:
        raise ValueError(
            f"Prototype 單次擷取不可超過 {MAX_PROTOTYPE_DURATION_SECONDS:.0f} 秒。"
        )
    if output_path.suffix.casefold() != ".wav":
        raise ValueError("Prototype 輸出檔案必須使用 .wav 副檔名。")


def capture_loopback_to_wav(
    *,
    duration_seconds: float,
    output_path: Path,
    device_index: int | None = None,
    frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
    on_stream_started: Callable[[], None] | None = None,
) -> CaptureResult:
    """Capture a bounded WASAPI loopback stream to a development WAV file."""

    output_path = output_path.expanduser().resolve()
    _validate_capture_request(duration_seconds, output_path)
    if frames_per_buffer <= 0:
        raise ValueError("frames_per_buffer 必須大於 0。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pyaudio = load_pyaudio_backend()
    capture_started = time.monotonic()

    try:
        with pyaudio.PyAudio() as audio_manager:
            device_info = _select_loopback_info(audio_manager, device_index)
            default_info = None
            try:
                default_info = audio_manager.get_default_wasapi_loopback()
            except (AttributeError, OSError):
                pass
            default_index = (
                int(default_info["index"])
                if isinstance(default_info, dict)
                else None
            )
            device = LoopbackDevice.from_mapping(
                device_info,
                default_index=default_index,
            )
            sample_width = int(audio_manager.get_sample_size(pyaudio.paInt16))
            target_frames = max(1, round(duration_seconds * device.sample_rate))
            source_frames_received = 0
            bytes_written = 0
            stream_status_event_count = 0
            statistics = _PcmS16LeStatistics()
            target_reached = threading.Event()
            callback_errors: list[BaseException] = []
            frame_width = sample_width * device.channels

            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(device.channels)
                wav_file.setsampwidth(sample_width)
                wav_file.setframerate(device.sample_rate)

                def audio_callback(
                    in_data: bytes,
                    frame_count: int,
                    _time_info: dict[str, float],
                    status_flags: int,
                ) -> tuple[bytes, int]:
                    nonlocal source_frames_received
                    nonlocal bytes_written
                    nonlocal stream_status_event_count
                    try:
                        if status_flags:
                            stream_status_event_count += 1
                        remaining_frames = target_frames - source_frames_received
                        accepted_frames = min(
                            max(0, int(frame_count)),
                            max(0, remaining_frames),
                        )
                        valid_bytes = in_data[: accepted_frames * frame_width]
                        actual_frames = len(valid_bytes) // frame_width
                        if actual_frames:
                            valid_bytes = valid_bytes[: actual_frames * frame_width]
                            wav_file.writeframesraw(valid_bytes)
                            statistics.add(valid_bytes)
                            source_frames_received += actual_frames
                            bytes_written += len(valid_bytes)
                        if source_frames_received >= target_frames:
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
                    if on_stream_started is not None:
                        on_stream_started()
                    target_reached.wait(timeout=duration_seconds)

                if callback_errors:
                    raise CaptureError(
                        "音訊 callback 失敗："
                        f"{type(callback_errors[0]).__name__}"
                    ) from callback_errors[0]

                silence_frames_padded = max(
                    0,
                    target_frames - source_frames_received,
                )
                remaining_padding_frames = silence_frames_padded
                silence_block = b"\x00" * (frames_per_buffer * frame_width)
                while remaining_padding_frames:
                    padding_frames = min(
                        frames_per_buffer,
                        remaining_padding_frames,
                    )
                    padding_bytes = silence_block[: padding_frames * frame_width]
                    wav_file.writeframesraw(padding_bytes)
                    statistics.add(padding_bytes)
                    bytes_written += len(padding_bytes)
                    remaining_padding_frames -= padding_frames

            wall_time = time.monotonic() - capture_started
            captured_frames = source_frames_received + silence_frames_padded
            return CaptureResult(
                output_path=str(output_path),
                device=device,
                requested_duration_seconds=duration_seconds,
                captured_duration_seconds=round(
                    captured_frames / device.sample_rate,
                    6,
                ),
                wall_time_seconds=round(wall_time, 6),
                frames_captured=captured_frames,
                source_frames_received=source_frames_received,
                silence_frames_padded=silence_frames_padded,
                bytes_written=bytes_written,
                stream_status_event_count=stream_status_event_count,
                peak_level=statistics.peak_level,
                rms_level=statistics.rms_level,
            )
    except (AudioBackendUnavailable, CaptureError):
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CaptureError(f"WASAPI Loopback 擷取失敗：{exc}") from exc
