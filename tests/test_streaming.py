from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.audio.chunking import AudioChunk
from app.audio.devices import LoopbackDevice
from app.audio.stream_capture import StreamCaptureResult
from app.transcription.engine import RuntimeInfo
from app.transcription.streaming import run_loopback_streaming_transcription
from app.transcription.streaming import StreamingTranscriptionError
from app.transcription.transcriber import TranscriptResult, TranscriptSegment


class _FakeChunkTranscriber:
    def __init__(self) -> None:
        self.engine = SimpleNamespace(
            runtime=RuntimeInfo(
                requested_device="cpu",
                resolved_device="cpu",
                compute_type="int8",
                cpu_threads=2,
                model_load_seconds=0.01,
                fallback_reason=None,
            )
        )

    def transcribe_file(self, source_path: Path, **_options) -> TranscriptResult:
        index = int(source_path.stem.split("_")[-1])
        text = "重複邊界" if index < 2 else "最後內容"
        return TranscriptResult(
            source_path=str(source_path),
            language="zh",
            language_probability=0.99,
            audio_duration_seconds=2.0,
            duration_after_vad_seconds=2.0,
            transcription_seconds=0.1,
            real_time_factor=0.05,
            runtime=self.engine.runtime,
            segments=(
                TranscriptSegment(
                    start=0.0,
                    end=1.0,
                    text=text,
                    average_log_probability=-0.1,
                    no_speech_probability=0.01,
                ),
            ),
        )


class StreamingPipelineTests(unittest.TestCase):
    def test_stops_capture_then_drains_queue_and_can_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_directory = Path(temporary_directory) / "session"

            def fake_capture(**options) -> StreamCaptureResult:
                spool_directory.mkdir(parents=True)
                for index, start_frame in enumerate((0, 15, 30)):
                    path = spool_directory / f"chunk_{index:06d}.wav"
                    with wave.open(str(path), "wb") as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(10)
                        wav_file.writeframes(b"\x00\x00" * 20)
                    options["on_chunk"](
                        AudioChunk(
                            index=index,
                            path=path,
                            start_frame=start_frame,
                            end_frame=start_frame + 20,
                            sample_rate=10,
                            channels=1,
                            sample_width=2,
                        )
                    )
                return StreamCaptureResult(
                    spool_directory=str(spool_directory),
                    device=LoopbackDevice(1, "test", 1, 10, True, 0),
                    requested_duration_seconds=5.0,
                    captured_duration_seconds=5.0,
                    wall_time_seconds=0.01,
                    frames_captured=50,
                    source_frames_received=50,
                    silence_frames_padded=0,
                    chunks_produced=3,
                    stream_status_event_count=0,
                )

            with patch(
                "app.transcription.streaming.capture_loopback_to_chunks",
                side_effect=fake_capture,
            ):
                run = run_loopback_streaming_transcription(
                    _FakeChunkTranscriber(),
                    duration_seconds=5.0,
                    spool_directory=spool_directory,
                    language="zh",
                    chunk_seconds=2.0,
                    overlap_seconds=0.5,
                )

            self.assertEqual(run.transcript.chunks_processed, 3)
            self.assertGreaterEqual(run.transcript.peak_queued_chunks, 1)
            self.assertEqual(
                run.transcript.peak_estimated_backlog_seconds,
                run.transcript.peak_queued_chunks * 2.0,
            )
            self.assertEqual(
                [segment.text for segment in run.transcript.segments],
                ["重複邊界", "最後內容"],
            )
            self.assertTrue(all(path.is_file() for path in run.chunk_paths))

            run.cleanup_chunks()

            self.assertFalse(spool_directory.exists())

    def test_transcription_failure_preserves_spooled_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_directory = Path(temporary_directory) / "failed-session"
            transcriber = _FakeChunkTranscriber()
            transcriber.transcribe_file = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("GPU failure")
            )

            def fake_capture(**options) -> StreamCaptureResult:
                spool_directory.mkdir(parents=True)
                path = spool_directory / "chunk_000000.wav"
                path.write_bytes(b"recoverable")
                options["on_chunk"](
                    AudioChunk(0, path, 0, 20, 10, 1, 2)
                )
                return StreamCaptureResult(
                    spool_directory=str(spool_directory),
                    device=LoopbackDevice(1, "test", 1, 10, True, 0),
                    requested_duration_seconds=2.0,
                    captured_duration_seconds=2.0,
                    wall_time_seconds=0.01,
                    frames_captured=20,
                    source_frames_received=20,
                    silence_frames_padded=0,
                    chunks_produced=1,
                    stream_status_event_count=0,
                )

            with patch(
                "app.transcription.streaming.capture_loopback_to_chunks",
                side_effect=fake_capture,
            ):
                with self.assertRaises(StreamingTranscriptionError):
                    run_loopback_streaming_transcription(
                        transcriber,
                        duration_seconds=2.0,
                        spool_directory=spool_directory,
                        language="zh",
                        chunk_seconds=2.0,
                        overlap_seconds=0.5,
                    )

            self.assertTrue((spool_directory / "chunk_000000.wav").is_file())

    def test_retries_failed_chunk_with_fallback_transcriber(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_directory = Path(temporary_directory) / "fallback-session"
            failing = _FakeChunkTranscriber()
            failing.transcribe_file = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("CUDA failure")
            )
            fallback = _FakeChunkTranscriber()

            def fake_capture(**options) -> StreamCaptureResult:
                spool_directory.mkdir(parents=True)
                path = spool_directory / "chunk_000000.wav"
                path.write_bytes(b"recoverable")
                options["on_chunk"](AudioChunk(0, path, 0, 20, 10, 1, 2))
                return StreamCaptureResult(
                    spool_directory=str(spool_directory),
                    device=LoopbackDevice(1, "test", 1, 10, True, 0),
                    requested_duration_seconds=2.0,
                    captured_duration_seconds=2.0,
                    wall_time_seconds=0.01,
                    frames_captured=20,
                    source_frames_received=20,
                    silence_frames_padded=0,
                    chunks_produced=1,
                    stream_status_event_count=0,
                )

            with patch(
                "app.transcription.streaming.capture_loopback_to_chunks",
                side_effect=fake_capture,
            ):
                run = run_loopback_streaming_transcription(
                    failing,
                    duration_seconds=2.0,
                    spool_directory=spool_directory,
                    language="zh",
                    chunk_seconds=2.0,
                    overlap_seconds=0.5,
                    fallback_transcriber_factory=lambda _error: fallback,
                )

            self.assertEqual(run.transcript.chunks_processed, 1)
            self.assertEqual(len(run.transcript.segments), 1)


if __name__ == "__main__":
    unittest.main()
