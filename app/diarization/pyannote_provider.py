from __future__ import annotations

import logging
import os
import wave
import warnings
from pathlib import Path
from typing import Any

import numpy as np

from app.diarization.models import DiarizationResult, SpeakerTurn
from app.diarization.provider import DiarizationError


DEFAULT_PYANNOTE_MODEL = "pyannote/speaker-diarization-community-1"
AUTO_MIN_SPEAKERS = 2
AUTO_MAX_SPEAKERS = 6
SHORT_TURN_DIAGNOSTIC_SECONDS = 0.1

_LOGGER = logging.getLogger("video_chapter_tool")
_SHORT_WINDOW_WARNING = "std(): degrees of freedom is <= 0"


def _anonymous_speaker_id(index: int) -> str:
    if index < 26:
        return f"speaker_{chr(ord('A') + index)}"
    return f"speaker_{index + 1}"


def _speaker_options(speaker_count: int | None) -> dict[str, int]:
    if speaker_count is not None:
        return {"num_speakers": speaker_count}
    return {
        "min_speakers": AUTO_MIN_SPEAKERS,
        "max_speakers": AUTO_MAX_SPEAKERS,
    }


def _windows_physical_core_count() -> int | None:
    """Read the Windows processor topology without CIM or deprecated WMIC."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_information = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        ).GetLogicalProcessorInformationEx
        get_information.argtypes = (
            wintypes.DWORD,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_information.restype = wintypes.BOOL
        relation_processor_core = 0
        required_bytes = wintypes.DWORD(0)
        get_information(
            relation_processor_core,
            None,
            ctypes.byref(required_bytes),
        )
        if required_bytes.value < 8:
            return None
        buffer = (ctypes.c_ubyte * required_bytes.value)()
        if not get_information(
            relation_processor_core,
            ctypes.byref(buffer),
            ctypes.byref(required_bytes),
        ):
            return None
        offset = 0
        core_count = 0
        while offset + 8 <= required_bytes.value:
            relationship = int.from_bytes(buffer[offset : offset + 4], "little")
            record_size = int.from_bytes(buffer[offset + 4 : offset + 8], "little")
            if record_size < 8 or offset + record_size > required_bytes.value:
                return None
            if relationship == relation_processor_core:
                core_count += 1
            offset += record_size
        return core_count or None
    except (AttributeError, OSError, ValueError):
        return None


def _configure_joblib_physical_cores() -> int | None:
    """Prime joblib's cache on Windows where WMIC is no longer installed."""
    core_count = _windows_physical_core_count()
    if core_count is None:
        return None
    try:
        from joblib.externals.loky.backend import context as loky_context
    except ImportError:
        return None
    if loky_context.physical_cores_cache is None:
        loky_context.physical_cores_cache = core_count
    return core_count


class PyannoteSpeakerDiarizer:
    """Lazy optional adapter; importing the application does not require PyTorch."""

    def __init__(
        self,
        *,
        model_source: str | Path = DEFAULT_PYANNOTE_MODEL,
        cache_directory: Path | None = None,
        requested_device: str = "auto",
        token: str | None = None,
    ) -> None:
        if requested_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("說話者分析裝置必須是 auto、cpu 或 cuda。")
        self.model_source = str(model_source)
        self.cache_directory = (
            cache_directory.expanduser().resolve()
            if cache_directory is not None
            else None
        )
        self.requested_device = requested_device
        self.token = token
        self._pipeline: Any | None = None
        self._resolved_device: str | None = None

    @property
    def provider_name(self) -> str:
        return "pyannote"

    @property
    def model_name(self) -> str:
        return self.model_source

    def _load(self) -> tuple[Any, str]:
        if self._pipeline is not None and self._resolved_device is not None:
            return self._pipeline, self._resolved_device
        if self.cache_directory is not None:
            matplotlib_cache = self.cache_directory / "Matplotlib"
            matplotlib_cache.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
            if os.name == "nt":
                # The managed model cache does not rely on symlinks. Avoid a
                # noisy Hugging Face warning on standard Windows accounts.
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            import torch

            # torch's FLOP counter is not used by diarization. CUDA builds can
            # emit a Triton warning merely because pyannote imports this module.
            logging.getLogger("torch.utils.flop_counter").setLevel(logging.ERROR)
            _configure_joblib_physical_cores()
            # We always pass an in-memory waveform, so TorchCodec/FFmpeg file
            # decoding is intentionally unused. pyannote documents this as a
            # supported fallback but emits a warning while importing its I/O
            # module when FFmpeg is absent.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"\s*torchcodec is not installed correctly.*",
                )
                from pyannote.audio import Pipeline
        except ImportError as exc:
            raise DiarizationError(
                "尚未安裝本機說話者分析元件；請先完成 Stage 6.5 "
                "pyannote 相容性安裝。"
            ) from exc

        if self.requested_device == "cuda" and not torch.cuda.is_available():
            raise DiarizationError("PyTorch 未偵測到可用的 NVIDIA CUDA 裝置。")
        resolved_device = (
            "cuda"
            if self.requested_device == "cuda"
            or (self.requested_device == "auto" and torch.cuda.is_available())
            else "cpu"
        )
        if resolved_device == "cuda":
            # pyannote disables TF32 for reproducible, accuracy-first output.
            # Apply the documented setting before loading the pipeline so it
            # does not need to emit a user-facing warning about doing so.
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        token = (
            self.token
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
        )
        kwargs: dict[str, Any] = {}
        if token:
            kwargs["token"] = token
        if self.cache_directory is not None:
            self.cache_directory.mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = str(self.cache_directory)
        try:
            pipeline = Pipeline.from_pretrained(self.model_source, **kwargs)
            if pipeline is None:
                raise RuntimeError("模型載入結果為空；請確認已接受模型使用條款。")
            pipeline.to(torch.device(resolved_device))
        except Exception as exc:
            raise DiarizationError(
                f"無法載入 pyannote 模型：{type(exc).__name__}: {exc}"
            ) from exc
        self._pipeline = pipeline
        self._resolved_device = resolved_device
        return pipeline, resolved_device

    def prepare(self) -> str:
        """Download/load the configured model and return the resolved device."""
        _, resolved_device = self._load()
        return resolved_device

    def analyze(
        self,
        audio_path: Path,
        *,
        speaker_count: int | None = None,
    ) -> DiarizationResult:
        audio_path = audio_path.expanduser().resolve()
        if not audio_path.is_file():
            raise DiarizationError(f"找不到說話者分析音訊：{audio_path}")
        if speaker_count is not None and not 1 <= speaker_count <= 6:
            raise ValueError("指定說話者人數必須介於 1～6。")
        pipeline, resolved_device = self._load()
        try:
            with wave.open(str(audio_path), "rb") as source:
                if source.getsampwidth() != 2:
                    raise RuntimeError("分析 WAV 必須是 16-bit PCM。")
                sample_rate = source.getframerate()
                channels = source.getnchannels()
                samples = np.frombuffer(
                    source.readframes(source.getnframes()),
                    dtype="<i2",
                )
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            waveform = np.asarray(samples, dtype=np.float32) / 32768.0
            import torch

            with warnings.catch_warnings(record=True) as recorded_warnings:
                warnings.simplefilter("always")
                output = pipeline(
                    {
                        "waveform": torch.from_numpy(waveform).unsqueeze(0),
                        "sample_rate": sample_rate,
                    },
                    **_speaker_options(speaker_count),
                )
            short_window_warning_count = 0
            for captured in recorded_warnings:
                if _SHORT_WINDOW_WARNING in str(captured.message):
                    short_window_warning_count += 1
                    continue
                warnings.warn_explicit(
                    captured.message,
                    captured.category,
                    captured.filename,
                    captured.lineno,
                )
            annotation = getattr(output, "exclusive_speaker_diarization", output)
            raw_turns = sorted(
                (
                    float(turn.start),
                    float(turn.end),
                    str(speaker),
                )
                for turn, _, speaker in annotation.itertracks(yield_label=True)
            )
            short_turn_count = sum(
                1
                for start, end, _ in raw_turns
                if 0 < end - start < SHORT_TURN_DIAGNOSTIC_SECONDS
            )
            if short_window_warning_count:
                _LOGGER.info(
                    "pyannote encountered %d short-window numeric warning(s); "
                    "analysis completed and retained %d sub-%.1fs speaker turn(s).",
                    short_window_warning_count,
                    short_turn_count,
                    SHORT_TURN_DIAGNOSTIC_SECONDS,
                )
        except Exception as exc:
            raise DiarizationError(
                f"pyannote 說話者分析失敗：{type(exc).__name__}: {exc}"
            ) from exc

        mapping: dict[str, str] = {}
        turns: list[SpeakerTurn] = []
        for start, end, original_speaker in raw_turns:
            if end <= start:
                continue
            speaker_id = mapping.setdefault(
                original_speaker,
                _anonymous_speaker_id(len(mapping)),
            )
            turns.append(
                SpeakerTurn(
                    start=round(start, 3),
                    end=round(end, 3),
                    speaker_id=speaker_id,
                )
            )
        if not turns:
            raise DiarizationError("pyannote 未產生任何說話者時間區段。")
        return DiarizationResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            resolved_device=resolved_device,
            turns=tuple(turns),
        )
