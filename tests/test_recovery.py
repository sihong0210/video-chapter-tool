from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from app.audio.chunking import AudioChunk
from app.database import SessionStore
from app.transcription.engine import RuntimeInfo
from app.transcription.recovery import RecoveryCancelled, recover_session_chunks
from app.transcription.transcriber import TranscriptResult, TranscriptSegment


class _RecoveryTranscriber:
    def __init__(self) -> None:
        self.engine = SimpleNamespace(
            runtime=RuntimeInfo("cpu", "cpu", "int8", 2, 0.01, None)
        )

    def transcribe_file(self, source_path: Path, **_options) -> TranscriptResult:
        return TranscriptResult(
            source_path=str(source_path),
            language="zh",
            language_probability=1.0,
            audio_duration_seconds=2.0,
            duration_after_vad_seconds=2.0,
            transcription_seconds=0.1,
            real_time_factor=0.05,
            runtime=self.engine.runtime,
            segments=(TranscriptSegment(0.0, 1.5, "復原成功", -0.1, 0.01),),
        )


class RecoveryTests(unittest.TestCase):
    def test_recovers_registered_wav_into_sqlite_then_deletes_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="復原測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
                chunk_seconds=2.0,
                overlap_seconds=0.5,
            )
            wav_path = root / "pending.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(10)
                wav_file.writeframes(b"\x00\x00" * 20)
            chunk = AudioChunk(0, wav_path, 0, 20, 10, 1, 2)
            store.register_chunk(session.id, chunk)
            store.update_status(session.id, "failed")

            result = recover_session_chunks(
                store,
                store.get_session(session.id),
                _RecoveryTranscriber(),
            )

            self.assertEqual(result.chunks_processed, 1)
            self.assertEqual(result.remaining_chunks, 0)
            self.assertFalse(wav_path.exists())
            self.assertEqual(
                store.get_transcript_segments(session.id)[0].text,
                "復原成功",
            )
            self.assertEqual(store.get_session(session.id).status, "transcript_ready")

    def test_completed_chunks_recover_duration_from_saved_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="強制關閉測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
            )
            chunk_path = root / "completed.wav"
            chunk_path.write_bytes(b"completed")
            chunk = AudioChunk(0, chunk_path, 0, 250, 10, 1, 2)
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (TranscriptSegment(2.0, 24.5, "已保存內容", -0.1, 0.01),),
            )
            chunk_path.unlink()
            store.update_status(session.id, "capturing")

            result = recover_session_chunks(
                store,
                store.get_session(session.id),
                _RecoveryTranscriber(),
            )

            self.assertEqual(result.chunks_processed, 0)
            self.assertEqual(result.remaining_chunks, 0)
            recovered = store.get_session(session.id)
            self.assertEqual(recovered.duration_seconds, 25.0)
            self.assertEqual(recovered.status, "transcript_ready")

    def test_cancel_stops_between_chunks_and_preserves_remaining_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="中止復原測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
                chunk_seconds=2.0,
                overlap_seconds=0.0,
            )
            for index in range(2):
                wav_path = root / f"pending_{index}.wav"
                with wave.open(str(wav_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(10)
                    wav_file.writeframes(b"\x00\x00" * 20)
                store.register_chunk(
                    session.id,
                    AudioChunk(index, wav_path, index * 20, (index + 1) * 20, 10, 1, 2),
                )
            progress = []

            with self.assertRaises(RecoveryCancelled):
                recover_session_chunks(
                    store,
                    store.get_session(session.id),
                    _RecoveryTranscriber(),
                    on_progress=progress.append,
                    should_cancel=lambda: bool(progress),
                )

            self.assertEqual(progress[0].chunks_processed, 1)
            remaining = store.list_pending_chunks(session.id)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].chunk_index, 1)
            self.assertTrue(Path(remaining[0].path).is_file())


if __name__ == "__main__":
    unittest.main()
