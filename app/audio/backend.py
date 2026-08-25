from __future__ import annotations

from types import ModuleType


class AudioBackendUnavailable(RuntimeError):
    """Raised when the optional Windows loopback backend is unavailable."""


def load_pyaudio_backend() -> ModuleType:
    try:
        import pyaudiowpatch as pyaudio
    except (ImportError, OSError) as exc:
        raise AudioBackendUnavailable(
            "無法載入 PyAudioWPatch。請在專案虛擬環境執行 "
            "`python -m pip install -r requirements.lock`。"
        ) from exc
    return pyaudio

