from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.audio.chunking import PcmChunkSpool


class PcmChunkSpoolTests(unittest.TestCase):
    def test_chunks_use_sample_clock_and_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = PcmChunkSpool(
                Path(temporary_directory),
                sample_rate=10,
                channels=1,
                sample_width=2,
                chunk_seconds=2.0,
                overlap_seconds=0.5,
            )

            chunks = list(spool.feed(b"\x01\x00" * 17))
            chunks.extend(spool.feed(b"\x01\x00" * 28))
            chunks.extend(spool.finalize())

            self.assertEqual(
                [(chunk.start_frame, chunk.end_frame) for chunk in chunks],
                [(0, 20), (15, 35), (30, 45)],
            )
            self.assertEqual(
                [chunk.start_seconds for chunk in chunks],
                [0.0, 1.5, 3.0],
            )
            for chunk, expected_frames in zip(chunks, (20, 20, 15)):
                with wave.open(str(chunk.path), "rb") as wav_file:
                    self.assertEqual(wav_file.getnframes(), expected_frames)

    def test_finalize_does_not_emit_overlap_only_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = PcmChunkSpool(
                Path(temporary_directory),
                sample_rate=10,
                channels=1,
                sample_width=2,
                chunk_seconds=2.0,
                overlap_seconds=0.5,
            )

            chunks = list(spool.feed(b"\x00\x00" * 35))
            chunks.extend(spool.finalize())

            self.assertEqual(len(chunks), 2)
            self.assertEqual(chunks[-1].end_frame, 35)

    def test_rejects_overlap_equal_to_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ValueError):
                PcmChunkSpool(
                    Path(temporary_directory),
                    sample_rate=48_000,
                    channels=2,
                    sample_width=2,
                    chunk_seconds=2.0,
                    overlap_seconds=2.0,
                )

    def test_low_disk_space_stops_before_writing_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = PcmChunkSpool(
                Path(temporary_directory),
                sample_rate=10,
                channels=1,
                sample_width=2,
                chunk_seconds=1.0,
                overlap_seconds=0.0,
            )
            with patch(
                "app.audio.chunking.shutil.disk_usage",
                return_value=SimpleNamespace(total=1_000, used=999, free=1),
            ):
                with self.assertRaisesRegex(OSError, "磁碟空間不足"):
                    spool.feed(b"\x00\x00" * 10)

            self.assertEqual(list(Path(temporary_directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
