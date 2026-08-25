from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from app.audio.chunking import AudioChunk
from app.config import AppPaths, AppSettings
from app.database import SessionStore
from app.models import ModelManager
from app.models.manager import REQUIRED_MODEL_FILES
from app.transcription import WhisperEngine
from app.transcription.engine import RuntimeInfo
from app.transcription.transcriber import TranscriptSegment
from app.workflows import (
    SessionRecoveryCallbacks,
    SessionRecoveryRequest,
    SessionRecoveryWorkflow,
)


class SessionRecoveryWorkflowTests(unittest.TestCase):
    def test_completed_transcript_is_finalized_without_loading_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            settings = AppSettings.defaults(paths)
            store = SessionStore(paths.database_file)
            session = store.create_session(
                title="GUI 復原測試",
                platform="test",
                source="system-audio",
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=paths.exports,
            )
            chunk = AudioChunk(
                0,
                paths.pending_audio / "completed.wav",
                0,
                20,
                1,
                1,
                2,
            )
            chunk.path.parent.mkdir(parents=True, exist_ok=True)
            chunk.path.write_bytes(b"already transcribed")
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (TranscriptSegment(0, 20, "已保存逐字稿", -0.1, 0.01),),
            )
            store.update_status(session.id, "capturing")

            def fail_if_loaded(*_args, **_options):
                self.fail("沒有待處理 Chunk 時不應載入 Whisper 模型")

            outcome = SessionRecoveryWorkflow(
                paths,
                settings,
                SessionRecoveryRequest(session.id),
                engine_loader=fail_if_loaded,
            ).run()

            self.assertFalse(outcome.cancelled)
            self.assertIsNotNone(outcome.export)
            self.assertTrue(outcome.export.txt_path.is_file())
            self.assertIn(
                "已保存逐字稿",
                outcome.export.txt_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                SessionStore(paths.database_file).get_session(session.id).status,
                "completed",
            )
            self.assertFalse(chunk.path.exists())

    def test_pending_audio_is_transcribed_reported_and_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            settings = AppSettings.defaults(paths)
            model_path = ModelManager(settings.model_directory).model_path("small")
            model_path.mkdir(parents=True, exist_ok=True)
            for filename in REQUIRED_MODEL_FILES:
                (model_path / filename).write_bytes(b"fake")
            store = SessionStore(paths.database_file)
            session = store.create_session(
                title="待處理音訊復原",
                platform="test",
                source="system-audio",
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=paths.exports,
                chunk_seconds=2,
                overlap_seconds=0,
            )
            wav_path = paths.pending_audio / "pending.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(10)
                wav_file.writeframes(b"\x00\x00" * 20)
            store.register_chunk(
                session.id,
                AudioChunk(0, wav_path, 0, 20, 10, 1, 2),
            )
            store.update_status(session.id, "failed")

            raw_segment = SimpleNamespace(
                start=0.0,
                end=1.5,
                text="復原後逐字稿",
                avg_logprob=-0.1,
                no_speech_prob=0.01,
                words=(),
            )
            info = SimpleNamespace(
                duration=2.0,
                duration_after_vad=2.0,
                language="zh",
                language_probability=1.0,
            )

            class FakeModel:
                def transcribe(self, *_args, **_options):
                    return iter((raw_segment,)), info

            progress = []

            def load_engine(*_args, **_options):
                return WhisperEngine(
                    FakeModel(),
                    RuntimeInfo("cpu", "cpu", "int8", 2, 0.01, None),
                )

            outcome = SessionRecoveryWorkflow(
                paths,
                settings,
                SessionRecoveryRequest(session.id),
                callbacks=SessionRecoveryCallbacks(on_progress=progress.append),
                engine_loader=load_engine,
            ).run()

            self.assertEqual(progress[-1].chunks_processed, 1)
            self.assertEqual(outcome.recovery.segments_added, 1)
            self.assertIn(
                "復原後逐字稿",
                outcome.export.txt_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(wav_path.exists())


if __name__ == "__main__":
    unittest.main()
