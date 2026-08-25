from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.audio.devices import LoopbackDevice
from app.audio.stream_capture import StreamCaptureResult
from app.stability.reporting import (
    MemorySample,
    ProgressSample,
    build_failed_report,
    build_stability_report,
)
from app.transcription.streaming import StreamingTranscriptResult
from app.transcription.transcriber import TranscriptSegment


def _transcript(
    *,
    source_frames: int = 180_000,
    requested_seconds: float = 1800.0,
    captured_seconds: float = 1800.0,
    stopped_by_request: bool = False,
) -> StreamingTranscriptResult:
    capture = StreamCaptureResult(
        spool_directory="pending",
        device=LoopbackDevice(1, "test", 1, 100, True, 0),
        requested_duration_seconds=requested_seconds,
        captured_duration_seconds=captured_seconds,
        wall_time_seconds=1800.1,
        frames_captured=180_000,
        source_frames_received=source_frames,
        silence_frames_padded=180_000 - source_frames,
        chunks_produced=2,
        stream_status_event_count=0,
        stopped_by_request=stopped_by_request,
    )
    return StreamingTranscriptResult(
        capture=capture,
        runtime={"resolved_device": "cuda", "compute_type": "float16"},
        language="zh",
        language_probability=0.99,
        transcription_seconds=10.0,
        session_wall_time_seconds=1801.0,
        chunks_processed=2,
        peak_queued_chunks=1,
        peak_estimated_backlog_seconds=25.0,
        segments=(
            TranscriptSegment(0.0, 2.0, "測試", -0.1, 0.01),
            TranscriptSegment(2.0, 4.0, "內容", -0.1, 0.01),
        ),
    )


class StabilityReportingTests(unittest.TestCase):
    def test_builds_passing_transcript_free_gate_report(self) -> None:
        observer = SimpleNamespace(
            memory_samples=(
                MemorySample(0.0, 500 * 1024**2),
                MemorySample(300.0, 505 * 1024**2),
                MemorySample(900.0, 506 * 1024**2),
                MemorySample(1800.0, 507 * 1024**2),
            ),
            progress_samples=(ProgressSample(1801.0, 2, 2, 0, 0.0),),
        )

        report = build_stability_report(
            session_id=7,
            transcript=_transcript(),
            observer=observer,
            pending_audio_files=0,
        )

        self.assertEqual(report["outcome"], "pass")
        self.assertEqual(report["checks"]["memory_growth"]["status"], "pass")
        self.assertFalse(report["privacy"]["contains_transcript"])
        self.assertNotIn("測試", str(report))

    def test_low_source_coverage_fails_gate(self) -> None:
        observer = SimpleNamespace(memory_samples=(), progress_samples=())

        report = build_stability_report(
            session_id=8,
            transcript=_transcript(source_frames=90_000),
            observer=observer,
            pending_audio_files=0,
        )

        self.assertEqual(report["outcome"], "fail")
        self.assertEqual(
            report["checks"]["source_audio_coverage"]["status"],
            "fail",
        )

    def test_manual_stop_uses_captured_time_for_gate_level(self) -> None:
        observer = SimpleNamespace(memory_samples=(), progress_samples=())

        report = build_stability_report(
            session_id=10,
            transcript=_transcript(
                requested_seconds=21_600.0,
                captured_seconds=300.0,
                stopped_by_request=True,
            ),
            observer=observer,
            pending_audio_files=0,
        )

        self.assertEqual(report["gate_level"], "short_baseline")
        self.assertEqual(
            report["checks"]["capture_duration"]["status"],
            "inconclusive",
        )

    def test_failed_report_has_diagnostics_without_transcript(self) -> None:
        observer = SimpleNamespace(memory_samples=(), progress_samples=())

        report = build_failed_report(
            session_id=9,
            observer=observer,
            error=RuntimeError("audio device removed"),
        )

        self.assertEqual(report["outcome"], "error")
        self.assertEqual(report["error"]["type"], "RuntimeError")
        self.assertFalse(report["privacy"]["contains_transcript"])


if __name__ == "__main__":
    unittest.main()
