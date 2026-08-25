from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from io import BytesIO
from pathlib import Path

from app.audio.capture import (
    MAX_PROTOTYPE_DURATION_SECONDS,
    _PcmS16LeStatistics,
    _validate_capture_request,
)
from app.audio.test_tone import build_test_tone_wav


class PcmStatisticsTests(unittest.TestCase):
    def test_silence_has_zero_levels(self) -> None:
        statistics = _PcmS16LeStatistics()
        statistics.add(b"\x00\x00" * 100)

        self.assertEqual(statistics.peak_level, 0.0)
        self.assertEqual(statistics.rms_level, 0.0)

    def test_known_pcm_values_produce_expected_levels(self) -> None:
        statistics = _PcmS16LeStatistics()
        samples = (32767, -32768, 0, 0)
        statistics.add(struct.pack("<hhhh", *samples))

        self.assertAlmostEqual(statistics.peak_level, 1.0, places=5)
        expected_rms = math.sqrt((32767**2 + 32768**2) / 4) / 32768
        self.assertAlmostEqual(statistics.rms_level, expected_rms, places=5)


class CaptureValidationTests(unittest.TestCase):
    def test_accepts_bounded_wav_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _validate_capture_request(
                5.0,
                Path(temporary_directory) / "測試.wav",
            )

    def test_rejects_invalid_duration(self) -> None:
        output = Path("test.wav")
        for duration in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    _validate_capture_request(duration, output)

    def test_rejects_unbounded_prototype_capture(self) -> None:
        with self.assertRaises(ValueError):
            _validate_capture_request(
                MAX_PROTOTYPE_DURATION_SECONDS + 1,
                Path("test.wav"),
            )

    def test_rejects_non_wav_output(self) -> None:
        with self.assertRaises(ValueError):
            _validate_capture_request(5.0, Path("test.mp3"))


class TestToneTests(unittest.TestCase):
    def test_generated_tone_is_a_bounded_valid_wav(self) -> None:
        tone = build_test_tone_wav(duration_seconds=0.1)
        with wave.open(BytesIO(tone), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 48_000)
            self.assertEqual(wav_file.getnframes(), 4_800)


if __name__ == "__main__":
    unittest.main()
