from __future__ import annotations

import logging
import os
import warnings
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def _package_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def collect_diarization_runtime(cache_directory: Path) -> dict[str, Any]:
    """Inspect the optional local runtime without exposing credentials."""
    cache_directory = cache_directory.expanduser().resolve()
    matplotlib_cache = cache_directory / "Matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))

    report: dict[str, Any] = {
        "pyannote_audio": _package_version("pyannote.audio"),
        "torch": _package_version("torch"),
        "torchaudio": _package_version("torchaudio"),
        "torchcodec": _package_version("torchcodec"),
        "hf_token_present": bool(
            os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        ),
        "cache_directory": str(cache_directory),
        "pyannote_import_ok": False,
        "cuda_available": False,
        "cuda_runtime": None,
        "gpu_name": None,
        "gpu_capability": None,
        "cuda_smoke_test": False,
        "error": None,
    }
    try:
        import torch

        # pyannote imports torch's optional FLOP counter. Triton is not needed
        # for diagnostics or diarization, so do not present its absence as a
        # user-facing warning.
        logging.getLogger("torch.utils.flop_counter").setLevel(logging.ERROR)

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"\s*torchcodec is not installed correctly.*",
            )
            from pyannote.audio import Pipeline  # noqa: F401

        report["pyannote_import_ok"] = True
        report["cuda_available"] = torch.cuda.is_available()
        report["cuda_runtime"] = torch.version.cuda
        if torch.cuda.is_available():
            report["gpu_name"] = torch.cuda.get_device_name(0)
            report["gpu_capability"] = ".".join(
                str(value) for value in torch.cuda.get_device_capability(0)
            )
            value = torch.ones(1, device="cuda")
            report["cuda_smoke_test"] = value.item() == 1.0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["ready_for_cpu"] = bool(report["pyannote_import_ok"])
    report["ready_for_cuda"] = bool(
        report["pyannote_import_ok"]
        and report["cuda_available"]
        and report["cuda_smoke_test"]
    )
    report["ready_to_download_model"] = bool(
        report["ready_for_cpu"] and report["hf_token_present"]
    )
    return report
