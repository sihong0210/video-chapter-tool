from __future__ import annotations

import ctypes
import json
import os
import platform
import sys
import wave
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.transcription.engine import (
    DISABLE_EXTERNAL_CUDA_DISCOVERY_ENV,
    WhisperEngine,
    _windows_cuda_dll_directories,
    cuda_preflight_error,
)


def _bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(sys.executable).resolve().parent


def _loaded_windows_library_path(library_name: str) -> Path:
    if os.name != "nt":
        raise RuntimeError("CUDA isolation DLL verification requires Windows.")

    library = ctypes.WinDLL(library_name)
    buffer = ctypes.create_unicode_buffer(32768)
    get_module_filename = ctypes.windll.kernel32.GetModuleFileNameW
    get_module_filename.argtypes = (
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    get_module_filename.restype = wintypes.DWORD
    length = get_module_filename(
        wintypes.HMODULE(library._handle),  # type: ignore[attr-defined]
        buffer,
        len(buffer),
    )
    if length <= 0:
        raise OSError(f"Unable to resolve loaded DLL path: {library_name}")
    return Path(buffer.value).resolve()


def _require_bundled_library(library_name: str, bundle_root: Path) -> Path:
    library_path = _loaded_windows_library_path(library_name)
    try:
        library_path.relative_to(bundle_root)
    except ValueError as exc:
        raise RuntimeError(
            f"CUDA isolation loaded {library_name} outside the bundle: "
            f"{library_path}"
        ) from exc
    return library_path


def _write_wav_prefix(source_path: Path, destination_path: Path, seconds: float) -> None:
    """Copy a short PCM WAV prefix for a bounded packaged diarization Gate."""

    with wave.open(str(source_path), "rb") as source:
        parameters = source.getparams()
        frame_count = min(
            source.getnframes(),
            max(1, round(source.getframerate() * seconds)),
        )
        frames = source.readframes(frame_count)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination_path), "wb") as destination:
        destination.setparams(parameters)
        destination.writeframes(frames)


def write_packaged_inference_test(
    report_path: Path,
    model_path: Path,
    *,
    requested_device: str,
    diarization_cache_directory: Path | None = None,
    diarization_audio_path: Path | None = None,
) -> bool:
    """Exercise packaged Whisper and optional diarization on CPU or CUDA."""

    if requested_device not in {"cpu", "cuda"}:
        raise ValueError("Packaged inference device must be cpu or cuda.")

    os.environ[DISABLE_EXTERNAL_CUDA_DISCOVERY_ENV] = "1"
    report_path = report_path.expanduser().resolve()
    model_path = model_path.expanduser().resolve()
    bundle_root = _bundle_root()
    payload: dict[str, Any] = {
        "status": "fail",
        "application_version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": platform.python_version(),
        "generated_at": datetime.now(UTC).isoformat(),
        "external_cuda_discovery_disabled": True,
        "bundle_root": str(bundle_root),
        "model_path": str(model_path),
        "requested_device": requested_device,
    }

    engine: WhisperEngine | None = None
    try:
        if not getattr(sys, "frozen", False):
            raise RuntimeError("CUDA isolation Gate must run from a packaged EXE.")
        if _windows_cuda_dll_directories():
            raise RuntimeError("External CUDA DLL directories were still discovered.")
        if not model_path.is_dir():
            raise FileNotFoundError(f"Whisper model directory not found: {model_path}")

        import numpy as np
        import torch

        if requested_device == "cuda":
            preflight_error = cuda_preflight_error()
            if preflight_error:
                raise RuntimeError(preflight_error)
            if not torch.cuda.is_available():
                raise RuntimeError("PyTorch did not detect an NVIDIA CUDA device.")

            matrix = torch.ones((32, 32), device="cuda")
            matrix_result = matrix @ matrix
            convolution = torch.nn.Conv1d(1, 2, kernel_size=3).to("cuda")
            convolution_result = convolution(torch.ones((1, 1, 32), device="cuda"))
            torch.cuda.synchronize()
            if float(matrix_result[0, 0].item()) != 32.0:
                raise RuntimeError("Unexpected PyTorch CUDA matrix result.")
            if tuple(convolution_result.shape) != (1, 2, 30):
                raise RuntimeError("Unexpected PyTorch CUDA convolution result.")

        engine = WhisperEngine.load(model_path, requested_device=requested_device)
        if engine.runtime.resolved_device != requested_device:
            raise RuntimeError(
                "Whisper packaged inference Gate unexpectedly selected "
                f"{engine.runtime.resolved_device}."
            )
        segments, information = engine.model.transcribe(
            np.zeros(16000, dtype=np.float32),
            language="zh",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        segment_count = len(list(segments))

        bundled_libraries: dict[str, str] = {}
        if requested_device == "cuda":
            bundled_libraries = {
                library_name: str(
                    _require_bundled_library(library_name, bundle_root)
                )
                for library_name in (
                    "cublas64_12.dll",
                    "cublasLt64_12.dll",
                    "cudnn64_9.dll",
                )
            }

        diarization_payload: dict[str, Any] | None = None
        if diarization_audio_path is not None:
            if diarization_cache_directory is None:
                raise RuntimeError(
                    "Diarization audio requires a diarization cache directory."
                )
            diarization_audio_path = diarization_audio_path.expanduser().resolve()
            diarization_cache_directory = (
                diarization_cache_directory.expanduser().resolve()
            )
            if not diarization_audio_path.is_file():
                raise FileNotFoundError(
                    f"Diarization Gate audio not found: {diarization_audio_path}"
                )
            if not diarization_cache_directory.is_dir():
                raise FileNotFoundError(
                    "Diarization model cache not found: "
                    f"{diarization_cache_directory}"
                )

            from app.diarization.pyannote_provider import PyannoteSpeakerDiarizer

            bounded_audio = report_path.parent / "diarization-gate-30s.wav"
            _write_wav_prefix(diarization_audio_path, bounded_audio, 30.0)
            try:
                os.environ["HF_HUB_OFFLINE"] = "1"
                diarizer = PyannoteSpeakerDiarizer(
                    cache_directory=diarization_cache_directory,
                    requested_device=requested_device,
                )
                diarization_result = diarizer.analyze(
                    bounded_audio,
                    speaker_count=2,
                )
                diarization_payload = {
                    "resolved_device": diarization_result.resolved_device,
                    "speaker_count": len(
                        {turn.speaker_id for turn in diarization_result.turns}
                    ),
                    "turn_count": len(diarization_result.turns),
                    "bounded_audio_seconds": 30.0,
                }
            finally:
                bounded_audio.unlink(missing_ok=True)
        payload.update(
            {
                "status": "pass",
                "gpu": (
                    torch.cuda.get_device_name(0)
                    if requested_device == "cuda"
                    else None
                ),
                "torch": torch.__version__,
                "torch_cuda_runtime": torch.version.cuda,
                "torch_cuda_available": torch.cuda.is_available(),
                "whisper_runtime": engine.runtime.to_dict(),
                "whisper_language": information.language,
                "whisper_segments": segment_count,
                "bundled_libraries": bundled_libraries,
                "diarization": diarization_payload,
            }
        )
    except Exception as exc:
        payload.update(
            {
                "error_type": type(exc).__name__,
                "error": " ".join(str(exc).split())[:1000],
            }
        )
    finally:
        if engine is not None:
            engine.close()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return payload["status"] == "pass"


def write_cuda_isolation_test(report_path: Path, model_path: Path) -> bool:
    """Exercise bundled CUDA runtimes without searching installed toolkits."""

    return write_packaged_inference_test(
        report_path,
        model_path,
        requested_device="cuda",
    )
