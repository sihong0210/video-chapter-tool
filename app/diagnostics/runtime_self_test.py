from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__


def _run_check(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"status": "pass", **operation()}
    except Exception as exc:  # diagnostic boundary: every dependency is isolated
        return {
            "status": "fail",
            "error_type": type(exc).__name__,
            "error": " ".join(str(exc).split())[:500],
        }


def _qt_check() -> dict[str, Any]:
    from PySide6 import QtCore

    return {"version": QtCore.__version__}


def _audio_check() -> dict[str, Any]:
    import pyaudiowpatch

    return {"module": pyaudiowpatch.__name__}


def _whisper_check() -> dict[str, Any]:
    import ctranslate2
    import faster_whisper

    return {
        "faster_whisper": getattr(faster_whisper, "__version__", "unknown"),
        "ctranslate2": ctranslate2.__version__,
        "cuda_devices": int(ctranslate2.get_cuda_device_count()),
    }


def _traditional_chinese_check() -> dict[str, Any]:
    from app.text_normalization import TaiwanTraditionalNormalizer

    converted = TaiwanTraditionalNormalizer()("软件和视频")
    if converted != "軟體和影片":
        raise RuntimeError(f"unexpected OpenCC output: {converted}")
    return {"sample": converted}


def _openai_check() -> dict[str, Any]:
    from openai import OpenAI

    return {"client": OpenAI.__name__}


def _diarization_check() -> dict[str, Any]:
    import torch
    from pyannote.audio import Pipeline

    return {
        "pipeline": Pipeline.__name__,
        "torch": torch.__version__,
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_runtime": torch.version.cuda,
    }


def write_runtime_self_test(report_path: Path) -> bool:
    checks = {
        "qt": _run_check(_qt_check),
        "system_audio": _run_check(_audio_check),
        "whisper": _run_check(_whisper_check),
        "traditional_chinese": _run_check(_traditional_chinese_check),
        "openai": _run_check(_openai_check),
        "speaker_diarization": _run_check(_diarization_check),
    }
    passed = all(check["status"] == "pass" for check in checks.values())
    payload = {
        "status": "pass" if passed else "fail",
        "application_version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    report_path = report_path.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return passed
