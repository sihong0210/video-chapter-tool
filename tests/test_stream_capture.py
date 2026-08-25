from __future__ import annotations

import unittest
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from app.audio.stream_capture import (
    _default_loopback_index,
    _required_silence_frames,
    capture_loopback_to_chunks,
)


class WallClockPaddingTests(unittest.TestCase):
    def test_long_callback_gap_is_padded_at_current_timeline(self) -> None:
        padding = _required_silence_frames(
            expected_timeline_frames=1200,
            timeline_frames=600,
            grace_frames=10,
            target_frames=1800,
        )

        self.assertEqual(padding, 590)

    def test_normal_callback_jitter_does_not_add_silence(self) -> None:
        padding = _required_silence_frames(
            expected_timeline_frames=620,
            timeline_frames=600,
            grace_frames=25,
            target_frames=1800,
        )

        self.assertEqual(padding, 0)

    def test_padding_never_exceeds_remaining_duration(self) -> None:
        padding = _required_silence_frames(
            expected_timeline_frames=2000,
            timeline_frames=1790,
            grace_frames=0,
            target_frames=1800,
        )

        self.assertEqual(padding, 10)

    def test_reads_default_loopback_index_for_device_change_checks(self) -> None:
        class FakeAudioManager:
            def get_default_wasapi_loopback(self):
                return {"index": "7"}

        self.assertEqual(_default_loopback_index(FakeAudioManager()), 7)

    def test_unavailable_default_loopback_is_not_misreported_as_change(self) -> None:
        class FakeAudioManager:
            def get_default_wasapi_loopback(self):
                raise OSError("temporarily unavailable")

        self.assertIsNone(_default_loopback_index(FakeAudioManager()))

    def test_ctrl_c_returns_partial_cancelled_capture(self) -> None:
        device = {
            "index": 7,
            "name": "test loopback",
            "maxInputChannels": 2,
            "defaultSampleRate": 48_000,
            "hostApi": 2,
            "isLoopbackDevice": True,
        }

        class FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeAudioManager:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get_default_wasapi_loopback(self):
                return device

            def get_loopback_device_info_generator(self):
                return iter((device,))

            def get_sample_size(self, _format):
                return 2

            def open(self, **_options):
                return FakeStream()

        class FakePyAudioModule:
            paInt16 = 8
            paComplete = 1
            paContinue = 0
            paAbort = 2

            @staticmethod
            def PyAudio():
                return FakeAudioManager()

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch(
                    "app.audio.stream_capture.load_pyaudio_backend",
                    return_value=FakePyAudioModule(),
                ),
                patch.object(
                    threading.Event,
                    "wait",
                    side_effect=KeyboardInterrupt,
                ),
            ):
                result = capture_loopback_to_chunks(
                    duration_seconds=60,
                    spool_directory=Path(temporary_directory),
                    on_chunk=lambda _chunk: None,
                    chunk_seconds=25,
                    overlap_seconds=2,
                )

        self.assertTrue(result.cancelled_by_user)
        self.assertLess(result.captured_duration_seconds, 1.0)


if __name__ == "__main__":
    unittest.main()
