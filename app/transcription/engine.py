from __future__ import annotations

import ctypes
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SUPPORTED_DEVICE_REQUESTS = {"auto", "cpu", "cuda", "amd_experimental"}
_DLL_DIRECTORY_HANDLES: list[Any] = []
_REGISTERED_DLL_DIRECTORIES: set[str] = set()


class EngineLoadError(RuntimeError):
    """Raised when no safe faster-whisper runtime can load a model."""


@dataclass(frozen=True, slots=True)
class RuntimeCandidate:
    device: str
    compute_type: str


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    requested_device: str
    resolved_device: str
    compute_type: str
    cpu_threads: int
    model_load_seconds: float
    fallback_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _windows_cuda_dll_directories() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()

    candidates: list[Path] = []
    for variable_name in ("CUDA_PATH", "CUDNN_PATH"):
        value = os.environ.get(variable_name)
        if value:
            root = Path(value)
            candidates.extend((root / "bin", root / "bin" / "x64"))

    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    cuda_root = program_files / "NVIDIA GPU Computing Toolkit" / "CUDA"
    cudnn_root = program_files / "NVIDIA" / "CUDNN"
    candidates.extend(
        version / "bin"
        for version in sorted(cuda_root.glob("v12.*"), reverse=True)
    )
    for version in sorted(cudnn_root.glob("v*"), reverse=True):
        candidates.extend((version / "bin", version / "bin" / "x64"))

    candidates.extend(
        Path(entry)
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    )

    existing: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen and resolved.is_dir():
            seen.add(key)
            existing.append(resolved)
    return tuple(existing)


def _configure_windows_cuda_dll_search() -> None:
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return

    for directory in _windows_cuda_dll_directories():
        key = os.path.normcase(str(directory))
        if key in _REGISTERED_DLL_DIRECTORIES:
            continue
        try:
            handle = os.add_dll_directory(str(directory))
        except OSError:
            continue
        _DLL_DIRECTORY_HANDLES.append(handle)
        _REGISTERED_DLL_DIRECTORIES.add(key)


def _cuda_device_count() -> int:
    _configure_windows_cuda_dll_search()
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except (ImportError, OSError, RuntimeError, ValueError):
        return 0


def cuda_preflight_error() -> str | None:
    _configure_windows_cuda_dll_search()
    if _cuda_device_count() <= 0:
        return "CTranslate2 未偵測到 CUDA 裝置。"
    if os.name != "nt":
        return None

    missing_libraries = []
    for library_name in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(library_name)
        except (AttributeError, OSError):
            missing_libraries.append(library_name)
    if missing_libraries:
        return "缺少 CUDA 執行期：" + ", ".join(missing_libraries)
    return None


def runtime_candidates(requested_device: str) -> tuple[RuntimeCandidate, ...]:
    if requested_device not in SUPPORTED_DEVICE_REQUESTS:
        raise ValueError(f"不支援的運算裝置：{requested_device}")

    cpu = RuntimeCandidate(device="cpu", compute_type="int8")
    if requested_device in {"cpu", "amd_experimental"}:
        return (cpu,)

    if _cuda_device_count() > 0:
        cuda = RuntimeCandidate(device="cuda", compute_type="float16")
        return (cuda, cpu)
    return (cpu,)


def _safe_error_summary(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    if len(message) > 240:
        message = message[:237] + "..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class WhisperEngine:
    def __init__(self, model: Any, runtime: RuntimeInfo) -> None:
        self.model = model
        self.runtime = runtime

    def close(self) -> None:
        """Release the native CTranslate2 model before another GPU stage."""
        self.model = None

    @classmethod
    def load(
        cls,
        model_path: Path,
        *,
        requested_device: str,
        cpu_threads: int | None = None,
    ) -> "WhisperEngine":
        model_path = model_path.expanduser().resolve()
        if not model_path.is_dir():
            raise EngineLoadError(f"模型資料夾不存在：{model_path}")

        try:
            from faster_whisper import WhisperModel
        except (ImportError, OSError) as exc:
            raise EngineLoadError("無法載入 faster-whisper 執行環境。") from exc

        effective_cpu_threads = cpu_threads or min(8, max(1, os.cpu_count() or 4))
        failures: list[str] = []
        candidates = runtime_candidates(requested_device)

        if candidates and candidates[0].device == "cuda":
            preflight_error = cuda_preflight_error()
            if preflight_error:
                failures.append(f"cuda/float16 preflight: {preflight_error}")
                candidates = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.device != "cuda"
                )

        for candidate in candidates:
            load_started = time.monotonic()
            try:
                model = WhisperModel(
                    str(model_path),
                    device=candidate.device,
                    compute_type=candidate.compute_type,
                    cpu_threads=effective_cpu_threads,
                    local_files_only=True,
                )
            except Exception as exc:
                failures.append(
                    f"{candidate.device}/{candidate.compute_type} "
                    f"{_safe_error_summary(exc)}"
                )
                continue

            fallback_reason = "; ".join(failures) if failures else None
            if requested_device == "amd_experimental":
                fallback_reason = (
                    "AMD ROCm/HIP 尚未列入正式 MVP 執行後端，已使用 CPU。"
                )
            runtime = RuntimeInfo(
                requested_device=requested_device,
                resolved_device=candidate.device,
                compute_type=candidate.compute_type,
                cpu_threads=effective_cpu_threads,
                model_load_seconds=round(time.monotonic() - load_started, 6),
                fallback_reason=fallback_reason,
            )
            return cls(model=model, runtime=runtime)

        detail = "; ".join(failures) or "沒有可用執行候選。"
        raise EngineLoadError(f"Whisper 模型載入失敗：{detail}")
