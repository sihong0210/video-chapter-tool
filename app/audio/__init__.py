"""Windows system-audio discovery and bounded loopback capture."""

from app.audio.capture import CaptureError, CaptureResult, capture_loopback_to_wav
from app.audio.chunking import AudioChunk, PcmChunkSpool
from app.audio.devices import (
    AudioBackendUnavailable,
    LoopbackDevice,
    get_loopback_devices,
)
from app.audio.stream_capture import (
    CaptureProgress,
    StreamCaptureResult,
    capture_loopback_to_chunks,
)

__all__ = [
    "AudioBackendUnavailable",
    "AudioChunk",
    "CaptureError",
    "CaptureResult",
    "CaptureProgress",
    "LoopbackDevice",
    "PcmChunkSpool",
    "StreamCaptureResult",
    "capture_loopback_to_wav",
    "capture_loopback_to_chunks",
    "get_loopback_devices",
]
