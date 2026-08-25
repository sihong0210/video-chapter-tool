from __future__ import annotations

import os
import tempfile
import unittest
import wave
from dataclasses import dataclass
from pathlib import Path

from app.audio.chunking import AudioChunk
from app.database import SessionStore
from app.diarization import (
    DiarizationResult,
    SpeakerTurn,
    analyze_session_speakers,
    assemble_session_wav,
    label_transcript_segments,
    validate_speaker_turns,
)
from app.transcription.transcriber import TranscriptSegment, TranscriptWord
from app.diarization.pyannote_provider import (
    SHORT_TURN_DIAGNOSTIC_SECONDS,
    _configure_joblib_physical_cores,
    _speaker_options,
    _windows_physical_core_count,
)


def _write_wav(path: Path, samples: tuple[int, ...], *, rate: int = 10) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(
            b"".join(int(value).to_bytes(2, "little", signed=True) for value in samples)
        )


@dataclass(frozen=True)
class _Chunk:
    path: str
    start_time: float
    end_time: float


class _FakeDiarizer:
    provider_name = "fake"
    model_name = "fake-two-speaker"

    def analyze(self, audio_path: Path, *, speaker_count: int | None = None):
        self.audio_existed = audio_path.is_file()
        self.speaker_count = speaker_count
        return DiarizationResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            resolved_device="cpu",
            turns=(
                SpeakerTurn(0.0, 1.0, "speaker_A"),
                SpeakerTurn(1.0, 2.0, "speaker_B"),
            ),
        )


class DiarizationTests(unittest.TestCase):
    def test_auto_speaker_count_is_bounded_to_multi_person_content(self) -> None:
        self.assertEqual(
            _speaker_options(None),
            {"min_speakers": 2, "max_speakers": 6},
        )
        self.assertEqual(_speaker_options(2), {"num_speakers": 2})
        self.assertEqual(SHORT_TURN_DIAGNOSTIC_SECONDS, 0.1)

    def test_windows_core_detection_primes_joblib_without_wmic(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-only processor topology helper")
        detected = _windows_physical_core_count()
        self.assertIsNotNone(detected)
        self.assertGreater(detected, 0)
        self.assertEqual(_configure_joblib_physical_cores(), detected)

    def test_rebuilds_timeline_without_chunk_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.wav"
            second = root / "second.wav"
            _write_wav(first, tuple(range(10)))
            _write_wav(second, tuple(range(8, 18)))

            output = assemble_session_wav(
                (
                    _Chunk(str(first), 0.0, 1.0),
                    _Chunk(str(second), 0.8, 1.8),
                ),
                root / "session.wav",
            )

            with wave.open(str(output), "rb") as source:
                self.assertEqual(source.getnframes(), 18)
                values = tuple(
                    int.from_bytes(source.readframes(1), "little", signed=True)
                    for _ in range(18)
                )
            self.assertEqual(values, tuple(range(18)))

    def test_rebuilds_mono_16khz_audio_for_pyannote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.wav"
            _write_wav(source_path, tuple(range(20)), rate=10)

            output = assemble_session_wav(
                (_Chunk(str(source_path), 0.0, 2.0),),
                root / "session.wav",
                target_sample_rate=16_000,
            )

            with wave.open(str(output), "rb") as source:
                self.assertEqual(source.getframerate(), 16_000)
                self.assertEqual(source.getnchannels(), 1)
                self.assertEqual(source.getsampwidth(), 2)
                self.assertEqual(source.getnframes(), 32_000)

    def test_labels_segments_by_largest_time_overlap(self) -> None:
        segments = (
            TranscriptSegment(
                0.0,
                1.2,
                "主持人發問",
                -0.1,
                0.01,
                words=(TranscriptWord(0.0, 0.5, "主持人", 0.9),),
            ),
            TranscriptSegment(1.2, 2.0, "來賓回答", -0.1, 0.01),
        )
        turns = (
            SpeakerTurn(0.0, 1.0, "speaker_A"),
            SpeakerTurn(1.0, 2.0, "speaker_B"),
        )

        labeled = label_transcript_segments(
            segments,
            turns,
            labels={"speaker_A": "主持人", "speaker_B": "來賓"},
        )

        self.assertEqual([item.speaker_name for item in labeled], ["主持人", "來賓"])

    def test_rejects_overlapping_exclusive_turns(self) -> None:
        with self.assertRaises(ValueError):
            validate_speaker_turns(
                (
                    SpeakerTurn(0.0, 2.0, "speaker_A"),
                    SpeakerTurn(1.5, 3.0, "speaker_B"),
                )
            )

    def test_session_pipeline_persists_labels_then_cleans_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="雙人直播",
                platform="web",
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
            )
            chunk_path = root / "pending" / "chunk.wav"
            chunk_path.parent.mkdir()
            _write_wav(chunk_path, tuple(range(20)))
            chunk = AudioChunk(0, chunk_path, 0, 20, 10, 1, 2)
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (
                    TranscriptSegment(0.0, 1.0, "甲說話", -0.1, 0.01),
                    TranscriptSegment(1.0, 2.0, "乙說話", -0.1, 0.01),
                ),
            )
            store.update_status(session.id, "transcript_ready", duration_seconds=2.0)
            diarizer = _FakeDiarizer()

            result = analyze_session_speakers(
                store,
                session.id,
                diarizer,
                working_directory=root / "analysis",
                speaker_count=2,
            )

            self.assertTrue(diarizer.audio_existed)
            self.assertEqual(diarizer.speaker_count, 2)
            self.assertEqual(len(store.get_speaker_turns(session.id)), 2)
            self.assertEqual(
                [item.display_name for item in store.get_speaker_labels(session.id)],
                ["說話者 A", "說話者 B"],
            )
            self.assertEqual(store.get_session(session.id).diarization_status, "completed")
            self.assertFalse(chunk_path.exists())
            self.assertEqual(result.chunks_cleaned, 1)


if __name__ == "__main__":
    unittest.main()
