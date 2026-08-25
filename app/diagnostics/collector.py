from __future__ import annotations

import os
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import __version__
from app.config.paths import AppPaths
from app.diagnostics.windows_hardware import (
    classify_gpu_vendor,
    get_audio_controllers,
    get_memory_info,
    get_video_controllers,
)


def _check_directory_writable(directory: Path) -> tuple[bool, str | None]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=True):
            pass
    except OSError as exc:
        return False, type(exc).__name__
    return True, None


def collect_diagnostics(paths: AppPaths) -> dict[str, Any]:
    """Collect support diagnostics without reading transcripts or secret settings."""

    warnings: list[str] = []

    memory, memory_warnings = get_memory_info()
    warnings.extend(memory_warnings)

    video_controllers, video_warnings = get_video_controllers()
    warnings.extend(video_warnings)
    normalized_gpu_map: dict[tuple[str, str], dict[str, Any]] = {}
    for device in video_controllers:
        name = str(device.get("Name") or "Unknown GPU")
        vendor = classify_gpu_vendor(name)
        key = (name.casefold(), vendor)
        if key in normalized_gpu_map:
            normalized_gpu_map[key]["detected_interfaces"] += 1
            continue
        normalized_gpu_map[key] = {
            "name": name,
            "vendor": vendor,
            "status": device.get("Status"),
            "driver_version": device.get("DriverVersion"),
            "detected_interfaces": 1,
        }
    normalized_gpus = list(normalized_gpu_map.values())

    audio_controllers, audio_warnings = get_audio_controllers()
    warnings.extend(audio_warnings)
    normalized_audio = [
        {
            "name": str(device.get("Name") or "Unknown audio device"),
            "status": device.get("Status"),
            "manufacturer": device.get("Manufacturer"),
        }
        for device in audio_controllers
    ]

    cache_writable, cache_error = _check_directory_writable(paths.cache)
    if cache_error:
        warnings.append(f"應用程式快取資料夾不可寫入：{cache_error}")

    gpu_vendors = {gpu["vendor"] for gpu in normalized_gpus}
    acceleration = {
        "recommended_for_mvp": "cpu",
        "nvidia_cuda_candidate": "nvidia" in gpu_vendors,
        "amd_rocm_experimental_candidate": "amd" in gpu_vendors,
        "note": "本結果僅偵測硬體；Stage 2 會實際驗證執行後端。",
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "application": {
            "name": "Video Chapter Tool",
            "version": __version__,
        },
        "operating_system": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "bits": platform.architecture()[0],
            "supported_by_project": (3, 11) <= sys.version_info[:2] < (3, 15),
        },
        "cpu": {
            "name": platform.processor()
            or os.environ.get("PROCESSOR_IDENTIFIER")
            or "Unknown CPU",
            "logical_cores": os.cpu_count(),
        },
        "memory": memory,
        "gpus": normalized_gpus,
        "audio_controllers": normalized_audio,
        "acceleration": acceleration,
        "storage": {
            "app_cache_writable": cache_writable,
        },
        "privacy": {
            "contains_transcript": False,
            "contains_api_keys": False,
            "contains_environment_dump": False,
        },
        "warnings": warnings,
    }
