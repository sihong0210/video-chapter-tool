from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.audio.devices import LoopbackDevice
from app.audio.stream_capture import StreamCaptureResult
from app.config import AppPaths, AppSettings
from app.database import SessionStore
from app.exporting import ExportError
from app.infrastructure import MemoryCredentialBackend, SecretStore
from app.transcription.engine import RuntimeInfo
from app.transcription.streaming import StreamingRun, StreamingTranscriptResult
from app.workflows import RecordingRequest, RecordingWorkflow, RecordingWorkflowError


class _FakeEngine:
    def __init__(self) -> None:
        self.runtime = RuntimeInfo(
            requested_device="auto",
            resolved_device="cuda",
            compute_type="float16",
            cpu_threads=4,
            model_load_seconds=0.01,
            fallback_reason=None,
        )
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ActivePowerRequest:
    active = True

    def __init__(self, **_options: object) -> None:
        pass

    def __enter__(self) -> "_ActivePowerRequest":
        return self

    def __exit__(self, *_args: object) -> None:
        pass


class RecordingWorkflowTests(unittest.TestCase):
    def test_manual_stop_workflow_persists_and_exports_empty_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            settings = AppSettings.defaults(paths)
            settings.speaker_mode = "off"
            fake_engine = _FakeEngine()
            capture = StreamCaptureResult(
                spool_directory=str(paths.pending_audio / "session_1"),
                device=LoopbackDevice(1, "test", 2, 48_000, True, 0),
                requested_duration_seconds=21_600.0,
                captured_duration_seconds=90.0,
                wall_time_seconds=90.1,
                frames_captured=4_320_000,
                source_frames_received=4_320_000,
                silence_frames_padded=0,
                chunks_produced=0,
                stream_status_event_count=0,
                stopped_by_request=True,
            )
            transcript = StreamingTranscriptResult(
                capture=capture,
                runtime={"resolved_device": "cuda", "compute_type": "float16"},
                language="zh",
                language_probability=None,
                transcription_seconds=0.0,
                session_wall_time_seconds=90.1,
                chunks_processed=0,
                peak_queued_chunks=0,
                peak_estimated_backlog_seconds=0.0,
                segments=(),
            )
            workflow = RecordingWorkflow(
                paths,
                settings,
                RecordingRequest("GUI 手動停止測試", keep_display_on=True),
                secret_store=SecretStore(MemoryCredentialBackend()),
            )
            workflow.request_stop()

            def fake_stream(*_args: object, **options: object) -> StreamingRun:
                self.assertIs(options["stop_event"], workflow.stop_event)
                self.assertTrue(workflow.stop_event.is_set())
                return StreamingRun(transcript=transcript, chunk_paths=())

            with (
                patch(
                    "app.workflows.recording.ModelManager.status",
                    return_value=SimpleNamespace(valid=True),
                ),
                patch(
                    "app.workflows.recording.ModelManager.model_path",
                    return_value=Path("model"),
                ),
                patch(
                    "app.workflows.recording.WhisperEngine.load",
                    return_value=fake_engine,
                ),
                patch(
                    "app.workflows.recording.RecordingPowerRequest",
                    _ActivePowerRequest,
                ),
                patch(
                    "app.workflows.recording.run_loopback_streaming_transcription",
                    side_effect=fake_stream,
                ),
            ):
                outcome = workflow.run()

            session = SessionStore(paths.database_file).get_session(outcome.session_id)
            self.assertEqual(session.status, "completed")
            self.assertEqual(session.duration_seconds, 90.0)
            self.assertTrue(outcome.export.txt_path.is_file())
            self.assertTrue(outcome.export.json_path.is_file())
            self.assertTrue(outcome.export.srt_path.is_file())
            self.assertTrue(fake_engine.closed)

    def test_export_failure_keeps_saved_transcript_reexportable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            settings = AppSettings.defaults(paths)
            settings.speaker_mode = "off"
            fake_engine = _FakeEngine()
            capture = StreamCaptureResult(
                spool_directory=str(paths.pending_audio / "session_1"),
                device=LoopbackDevice(1, "test", 2, 48_000, True, 0),
                requested_duration_seconds=21_600.0,
                captured_duration_seconds=6.0,
                wall_time_seconds=6.1,
                frames_captured=288_000,
                source_frames_received=288_000,
                silence_frames_padded=0,
                chunks_produced=0,
                stream_status_event_count=0,
                stopped_by_request=True,
            )
            transcript = StreamingTranscriptResult(
                capture=capture,
                runtime={"resolved_device": "cuda", "compute_type": "float16"},
                language="zh",
                language_probability=None,
                transcription_seconds=0.0,
                session_wall_time_seconds=6.1,
                chunks_processed=0,
                peak_queued_chunks=0,
                peak_estimated_backlog_seconds=0.0,
                segments=(),
            )
            workflow = RecordingWorkflow(
                paths,
                settings,
                RecordingRequest("匯出失敗保留測試", keep_display_on=True),
                secret_store=SecretStore(MemoryCredentialBackend()),
            )

            with (
                patch(
                    "app.workflows.recording.ModelManager.status",
                    return_value=SimpleNamespace(valid=True),
                ),
                patch(
                    "app.workflows.recording.ModelManager.model_path",
                    return_value=Path("model"),
                ),
                patch(
                    "app.workflows.recording.WhisperEngine.load",
                    return_value=fake_engine,
                ),
                patch(
                    "app.workflows.recording.RecordingPowerRequest",
                    _ActivePowerRequest,
                ),
                patch(
                    "app.workflows.recording.run_loopback_streaming_transcription",
                    return_value=StreamingRun(transcript=transcript, chunk_paths=()),
                ),
                patch(
                    "app.workflows.recording.export_session",
                    side_effect=ExportError("disk unavailable"),
                ),
            ):
                with self.assertRaises(RecordingWorkflowError):
                    workflow.run()

            session = SessionStore(paths.database_file).list_sessions()[0]
            self.assertEqual(session.status, "transcript_ready")
            self.assertIsNone(session.exported_path)
            self.assertIn("匯出尚未完成", session.error_message or "")
            self.assertTrue(fake_engine.closed)


if __name__ == "__main__":
    unittest.main()
