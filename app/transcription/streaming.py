from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.audio.chunking import AudioChunk
from app.audio.stream_capture import (
    CaptureProgress,
    StreamCaptureResult,
    capture_loopback_to_chunks,
)
from app.transcription.merge import TranscriptMerger
from app.transcription.transcriber import (
    BasicTranscriber,
    TranscriptSegment,
)


class StreamingTranscriptionError(RuntimeError):
    """Raised when a streaming session cannot finish without losing recovery data."""


@dataclass(frozen=True, slots=True)
class StreamProgress:
    chunks_produced: int
    chunks_processed: int
    queued_chunks: int
    estimated_backlog_seconds: float


@dataclass(frozen=True, slots=True)
class StreamingTranscriptResult:
    capture: StreamCaptureResult
    runtime: dict[str, Any]
    language: str | None
    language_probability: float | None
    transcription_seconds: float
    session_wall_time_seconds: float
    chunks_processed: int
    peak_queued_chunks: int
    peak_estimated_backlog_seconds: float
    segments: tuple[TranscriptSegment, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["capture"] = self.capture.to_dict()
        result["segments"] = [segment.to_dict() for segment in self.segments]
        return result


@dataclass(frozen=True, slots=True)
class StreamingRun:
    transcript: StreamingTranscriptResult
    chunk_paths: tuple[Path, ...]

    def cleanup_chunks(self) -> None:
        for chunk_path in self.chunk_paths:
            chunk_path.unlink(missing_ok=True)
        spool_directory = Path(self.transcript.capture.spool_directory)
        try:
            spool_directory.rmdir()
        except OSError:
            pass


def run_loopback_streaming_transcription(
    transcriber: BasicTranscriber,
    *,
    duration_seconds: float,
    spool_directory: Path,
    language: str | None,
    device_index: int | None = None,
    chunk_seconds: float = 25.0,
    overlap_seconds: float = 2.0,
    beam_size: int = 5,
    vad_filter: bool = True,
    word_timestamps: bool = False,
    on_stream_started: Callable[[], None] | None = None,
    on_progress: Callable[[StreamProgress], None] | None = None,
    on_chunk_spooled: Callable[[AudioChunk], None] | None = None,
    on_chunk_transcribed: (
        Callable[[AudioChunk, tuple[TranscriptSegment, ...]], None] | None
    ) = None,
    on_chunk_failed: Callable[[AudioChunk, BaseException], None] | None = None,
    on_capture_complete: Callable[[StreamCaptureResult], None] | None = None,
    fallback_transcriber_factory: (
        Callable[[BaseException], BasicTranscriber | None] | None
    ) = None,
    on_capture_progress: Callable[[CaptureProgress], None] | None = None,
    stop_event: threading.Event | None = None,
) -> StreamingRun:
    """Capture and transcribe concurrently, retaining chunks until output is saved."""

    work_queue: queue.Queue[AudioChunk | object] = queue.Queue()
    sentinel = object()
    merger = TranscriptMerger(overlap_seconds=overlap_seconds)
    chunk_paths: list[Path] = []
    worker_errors: list[BaseException] = []
    language_values: list[tuple[str, float | None]] = []
    transcription_seconds = 0.0
    chunks_produced = 0
    chunks_processed = 0
    peak_queued_chunks = 0
    queued_audio_chunks = 0
    metrics_lock = threading.Lock()
    session_started = time.monotonic()
    active_transcriber = transcriber
    fallback_attempted = False

    def report_progress() -> None:
        if on_progress is None:
            return
        with metrics_lock:
            progress = StreamProgress(
                chunks_produced=chunks_produced,
                chunks_processed=chunks_processed,
                queued_chunks=queued_audio_chunks,
                estimated_backlog_seconds=round(
                    queued_audio_chunks * chunk_seconds,
                    3,
                ),
            )
        try:
            on_progress(progress)
        except Exception:
            # Progress reporting must never stop capture or transcript recovery.
            pass

    def enqueue_chunk(chunk: AudioChunk) -> None:
        nonlocal chunks_produced
        nonlocal peak_queued_chunks
        nonlocal queued_audio_chunks
        chunk_paths.append(chunk.path)
        if on_chunk_spooled is not None:
            on_chunk_spooled(chunk)
        with metrics_lock:
            chunks_produced += 1
            queued_audio_chunks += 1
            peak_queued_chunks = max(peak_queued_chunks, queued_audio_chunks)
            work_queue.put(chunk)

    def worker() -> None:
        nonlocal active_transcriber
        nonlocal chunks_processed
        nonlocal fallback_attempted
        nonlocal queued_audio_chunks
        nonlocal transcription_seconds
        while True:
            item = work_queue.get()
            try:
                if item is sentinel:
                    return
                assert isinstance(item, AudioChunk)
                with metrics_lock:
                    queued_audio_chunks = max(0, queued_audio_chunks - 1)
                if worker_errors:
                    continue
                try:
                    result = active_transcriber.transcribe_file(
                        item.path,
                        language=language,
                        beam_size=beam_size,
                        vad_filter=vad_filter,
                        word_timestamps=word_timestamps,
                    )
                except Exception as exc:
                    if (
                        fallback_transcriber_factory is None
                        or fallback_attempted
                    ):
                        worker_errors.append(exc)
                        if on_chunk_failed is not None:
                            try:
                                on_chunk_failed(item, exc)
                            except Exception:
                                pass
                        continue
                    fallback_attempted = True
                    try:
                        fallback = fallback_transcriber_factory(exc)
                        if fallback is None:
                            raise exc
                        active_transcriber = fallback
                        result = active_transcriber.transcribe_file(
                            item.path,
                            language=language,
                            beam_size=beam_size,
                            vad_filter=vad_filter,
                            word_timestamps=word_timestamps,
                        )
                    except Exception as fallback_exc:
                        worker_errors.append(fallback_exc)
                        if on_chunk_failed is not None:
                            try:
                                on_chunk_failed(item, fallback_exc)
                            except Exception:
                                pass
                        continue
                try:
                    added_segments = merger.add_chunk(
                        item.start_seconds,
                        result.segments,
                        chunk_end_seconds=item.end_seconds,
                    )
                    if on_chunk_transcribed is not None:
                        on_chunk_transcribed(item, added_segments)
                except Exception as persist_exc:
                    worker_errors.append(persist_exc)
                    if on_chunk_failed is not None:
                        try:
                            on_chunk_failed(item, persist_exc)
                        except Exception:
                            pass
                    continue
                if result.language:
                    language_values.append(
                        (result.language, result.language_probability)
                    )
                with metrics_lock:
                    chunks_processed += 1
                    transcription_seconds += result.transcription_seconds
                report_progress()
            finally:
                work_queue.task_done()

    worker_thread = threading.Thread(
        target=worker,
        name="whisper-chunk-consumer",
        daemon=False,
    )
    worker_thread.start()
    capture_result: StreamCaptureResult | None = None
    capture_error: BaseException | None = None
    try:
        capture_result = capture_loopback_to_chunks(
            duration_seconds=duration_seconds,
            spool_directory=spool_directory,
            on_chunk=enqueue_chunk,
            device_index=device_index,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            on_stream_started=on_stream_started,
            on_capture_progress=on_capture_progress,
            stop_event=stop_event,
        )
        if on_capture_complete is not None:
            on_capture_complete(capture_result)
    except BaseException as exc:
        capture_error = exc
    finally:
        work_queue.put(sentinel)
        work_queue.join()
        worker_thread.join()

    if capture_error is not None:
        raise StreamingTranscriptionError(
            f"串流擷取失敗；待處理音訊保留於 {spool_directory}："
            f"{capture_error}"
        ) from capture_error
    if worker_errors:
        error = worker_errors[0]
        raise StreamingTranscriptionError(
            f"Chunk 轉錄失敗；待處理音訊保留於 {spool_directory}："
            f"{type(error).__name__}: {error}"
        ) from error
    if capture_result is None:
        raise StreamingTranscriptionError("串流工作未產生擷取結果。")

    detected_language = language_values[0][0] if language_values else language
    detected_probability = language_values[0][1] if language_values else None
    runtime = active_transcriber.engine.runtime.to_dict()
    transcript = StreamingTranscriptResult(
        capture=capture_result,
        runtime=runtime,
        language=detected_language,
        language_probability=detected_probability,
        transcription_seconds=round(transcription_seconds, 6),
        session_wall_time_seconds=round(time.monotonic() - session_started, 6),
        chunks_processed=chunks_processed,
        peak_queued_chunks=peak_queued_chunks,
        peak_estimated_backlog_seconds=round(
            peak_queued_chunks * chunk_seconds,
            3,
        ),
        segments=merger.segments,
    )
    return StreamingRun(transcript=transcript, chunk_paths=tuple(chunk_paths))
