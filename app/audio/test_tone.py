from __future__ import annotations

import io
import math
import struct
import threading
import wave


def build_test_tone_wav(
    *,
    duration_seconds: float = 0.5,
    frequency_hz: float = 880.0,
    volume: float = 0.08,
    sample_rate: int = 48_000,
) -> bytes:
    """Build a quiet mono WAV used only for manual loopback verification."""

    if duration_seconds <= 0 or frequency_hz <= 0 or sample_rate <= 0:
        raise ValueError("測試音參數必須大於 0。")
    if not 0 < volume <= 0.25:
        raise ValueError("測試音音量必須介於 0 與 0.25 之間。")

    frame_count = round(duration_seconds * sample_rate)
    amplitude = round(32767 * volume)
    pcm = bytearray()
    for frame_index in range(frame_count):
        sample = round(
            amplitude
            * math.sin(2 * math.pi * frequency_hz * frame_index / sample_rate)
        )
        pcm.extend(struct.pack("<h", sample))

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def play_test_tone_async() -> threading.Thread:
    """Play a short, low-volume Windows test tone without blocking capture."""

    if not hasattr(threading, "Thread"):
        raise RuntimeError("目前環境無法建立測試音執行緒。")

    tone_wav = build_test_tone_wav()

    def play() -> None:
        import winsound

        winsound.PlaySound(tone_wav, winsound.SND_MEMORY | winsound.SND_SYNC)

    thread = threading.Thread(
        target=play,
        name="loopback-test-tone",
        daemon=True,
    )
    thread.start()
    return thread

