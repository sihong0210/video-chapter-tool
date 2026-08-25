from __future__ import annotations

import gc
import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.audio import CaptureProgress
from app.config import AppPaths, AppSettings
from app.database import DatabaseError, SessionStore
from app.diarization import (
    DEFAULT_PYANNOTE_MODEL,
    DiarizationAudioError,
    DiarizationError,
    PyannoteSpeakerDiarizer,
    analyze_session_speakers,
)
from app.exporting import ExportError, ExportResult, export_session
from app.infrastructure import (
    HUGGINGFACE_SECRET,
    RecordingPowerRequest,
    SecretStore,
)
from app.models import ModelManager
from app.stability import StabilityObserver, build_failed_report, build_stability_report
from app.text_normalization import build_text_normalizer
from app.transcription import (
    BasicTranscriber,
    StreamingTranscriptionError,
    TranscriptWord,
    WhisperEngine,
    run_loopback_streaming_transcription,
)
from app.transcription.streaming import StreamProgress, StreamingRun
from app.transcription.transcriber import TranscriptSegment


# The GUI is presented as "until manually stopped". This high internal ceiling
# protects against an abandoned process and matches the already validated core.
GUI_SAFETY_DURATION_SECONDS = 21_600.0


class RecordingWorkflowError(RuntimeError):
    """Raised after recoverable state has been persisted for a GUI recording."""


@dataclass(frozen=True, slots=True)
class RecordingRequest:
    title: str
    keep_display_on: bool
    device_index: int | None = None
    chunk_seconds: float = 25.0
    overlap_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class RecordingOutcome:
    session_id: int
    captured_duration_seconds: float
    chunks_processed: int
    segments: int
    export: ExportResult
    speaker_turns: int
    warning: str | None = None


@dataclass(slots=True)
class RecordingCallbacks:
    on_status: Callable[[str], None] | None = None
    on_capture_progress: Callable[[CaptureProgress], None] | None = None
    on_stream_progress: Callable[[StreamProgress], None] | None = None
    on_segments: Callable[[tuple[TranscriptSegment, ...]], None] | None = None
    on_session_created: Callable[[int], None] | None = None


def _emit(callback: Callable[..., None] | None, *args: object) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        # UI feedback must never break persistence or recovery.
        pass


def _write_json_atomic(path: Path, payload: dict[str, object]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return path


class RecordingWorkflow:
    def __init__(
        self,
        paths: AppPaths,
        settings: AppSettings,
        request: RecordingRequest,
        *,
        callbacks: RecordingCallbacks | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.request = request
        self.callbacks = callbacks or RecordingCallbacks()
        self.secret_store = secret_store or SecretStore()
        self.stop_event = threading.Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    def _preview_segments(
        self,
        segments: tuple[TranscriptSegment, ...],
        normalizer: Callable[[str], str] | None,
    ) -> tuple[TranscriptSegment, ...]:
        if normalizer is None:
            return segments
        return tuple(
            replace(
                segment,
                text=normalizer(segment.text),
                words=tuple(
                    replace(word, text=normalizer(word.text))
                    if isinstance(word, TranscriptWord)
                    else word
                    for word in segment.words
                ),
            )
            for segment in segments
        )

    def run(self) -> RecordingOutcome:
        title = self.request.title.strip() or datetime.now().strftime(
            "直播轉錄 %Y-%m-%d %H-%M-%S"
        )
        manager = ModelManager(self.settings.model_directory)
        model_status = manager.status(self.settings.whisper_model)
        if not model_status.valid:
            raise RecordingWorkflowError(
                f"Whisper 模型 {self.settings.whisper_model} 尚未完成下載或驗證。"
            )
        language = None if self.settings.language == "auto" else self.settings.language
        normalizer = build_text_normalizer(
            language=language,
            traditional_chinese_output=self.settings.traditional_chinese_output,
        )
        speaker_enabled = self.settings.speaker_mode != "off"
        speaker_count = (
            self.settings.speaker_count
            if self.settings.speaker_mode == "fixed"
            else None
        )
        store = SessionStore(self.paths.database_file)
        session = store.create_session(
            title=title,
            platform="gui",
            source="Windows 系統音訊",
            language=language,
            model_name=self.settings.whisper_model,
            requested_device=self.settings.compute_device,
            export_directory=Path(self.settings.export_directory),
            chunk_seconds=self.request.chunk_seconds,
            overlap_seconds=self.request.overlap_seconds,
        )
        _emit(self.callbacks.on_session_created, session.id)
        if speaker_enabled:
            store.set_diarization_status(
                session.id,
                "pending",
                provider_name="pyannote",
                model_name=DEFAULT_PYANNOTE_MODEL,
                requested_speaker_count=speaker_count,
            )
        spool_directory = self.paths.pending_audio / f"session_{session.id}"
        report_path = self.paths.stability_reports / f"session_{session.id}_stability.json"
        observer = StabilityObserver()
        engine: WhisperEngine | None = None
        run: StreamingRun | None = None
        observer_started = False
        try:
            _emit(self.callbacks.on_status, "正在載入 Whisper 模型…")
            engine = WhisperEngine.load(
                manager.model_path(self.settings.whisper_model),
                requested_device=self.settings.compute_device,
            )
            store.update_runtime(
                session.id,
                resolved_device=engine.runtime.resolved_device,
                compute_type=engine.runtime.compute_type,
            )
            store.update_status(session.id, "capturing")
            transcriber = BasicTranscriber(engine)
            observer.start()
            observer_started = True

            def build_cpu_fallback(error: BaseException) -> BasicTranscriber | None:
                if engine is None or engine.runtime.resolved_device != "cuda":
                    return None
                cpu_engine = WhisperEngine.load(
                    manager.model_path(self.settings.whisper_model),
                    requested_device="cpu",
                )
                cpu_engine.runtime = replace(
                    cpu_engine.runtime,
                    requested_device=self.settings.compute_device,
                    fallback_reason=(
                        "CUDA 串流推論失敗，已改用 CPU INT8："
                        + " ".join(str(error).split())[:240]
                    ),
                )
                return BasicTranscriber(cpu_engine)

            def persist_chunk(chunk: object) -> None:
                store.register_chunk(session.id, chunk)  # type: ignore[arg-type]

            def persist_segments(
                chunk: object,
                segments: tuple[TranscriptSegment, ...],
            ) -> None:
                store.commit_chunk_transcript(
                    session.id,
                    chunk,  # type: ignore[arg-type]
                    segments,
                    normalize_text=normalizer,
                )
                _emit(
                    self.callbacks.on_segments,
                    self._preview_segments(segments, normalizer),
                )
                if not speaker_enabled:
                    try:
                        chunk.path.unlink(missing_ok=True)  # type: ignore[attr-defined]
                    except OSError:
                        pass

            def persist_failure(chunk: object, error: BaseException) -> None:
                store.mark_chunk_failed(
                    session.id,
                    chunk.index,  # type: ignore[attr-defined]
                    " ".join(str(error).split())[:500],
                )

            def capture_complete(capture: object) -> None:
                store.update_status(
                    session.id,
                    "draining_queue",
                    duration_seconds=capture.captured_duration_seconds,  # type: ignore[attr-defined]
                )
                _emit(self.callbacks.on_status, "擷取已停止，正在處理剩餘音訊…")

            def stream_progress(progress: StreamProgress) -> None:
                observer.record_progress(progress)
                _emit(self.callbacks.on_stream_progress, progress)

            def stream_started() -> None:
                _emit(self.callbacks.on_status, "錄製中；現在可以播放直播或影片。")

            with RecordingPowerRequest(
                keep_display_on=self.request.keep_display_on
            ) as power_request:
                if not power_request.active:
                    _emit(
                        self.callbacks.on_status,
                        "錄製中；Windows 防休眠要求未生效，請保持電腦喚醒。",
                    )
                run = run_loopback_streaming_transcription(
                    transcriber,
                    duration_seconds=GUI_SAFETY_DURATION_SECONDS,
                    spool_directory=spool_directory,
                    language=language,
                    device_index=self.request.device_index,
                    chunk_seconds=self.request.chunk_seconds,
                    overlap_seconds=self.request.overlap_seconds,
                    word_timestamps=speaker_enabled,
                    on_stream_started=stream_started,
                    on_capture_progress=lambda progress: _emit(
                        self.callbacks.on_capture_progress,
                        progress,
                    ),
                    on_progress=stream_progress,
                    on_chunk_spooled=persist_chunk,
                    on_chunk_transcribed=persist_segments,
                    on_chunk_failed=persist_failure,
                    on_capture_complete=capture_complete,
                    fallback_transcriber_factory=build_cpu_fallback,
                    stop_event=self.stop_event,
                )
            observer.stop()
            observer_started = False
        except Exception as exc:
            if observer_started:
                observer.stop()
            try:
                _write_json_atomic(
                    report_path,
                    build_failed_report(session_id=session.id, observer=observer, error=exc),
                )
                store.set_stability_report_path(session.id, report_path)
            except (DatabaseError, OSError):
                pass
            try:
                store.update_status(
                    session.id,
                    "failed",
                    error_message=" ".join(str(exc).split())[:500],
                )
            except DatabaseError:
                pass
            raise RecordingWorkflowError(
                f"錄製工作失敗；工作階段 #{session.id} 與可復原音訊已保留：{exc}"
            ) from exc
        finally:
            if engine is not None:
                engine.close()

        if run is None:
            raise RecordingWorkflowError("錄製工作沒有產生結果。")

        warning: str | None = None
        speaker_turns = 0
        store.update_runtime(
            session.id,
            resolved_device=run.transcript.runtime["resolved_device"],
            compute_type=run.transcript.runtime["compute_type"],
        )
        store.update_status(
            session.id,
            "transcript_ready",
            duration_seconds=run.transcript.capture.captured_duration_seconds,
        )
        if speaker_enabled:
            _emit(self.callbacks.on_status, "逐字稿完成，正在分析說話者 A／B…")
            gc.collect()
            diarizer = PyannoteSpeakerDiarizer(
                model_source=DEFAULT_PYANNOTE_MODEL,
                cache_directory=Path(self.settings.model_directory) / "Diarization",
                requested_device=self.settings.diarization_device,
                token=self.secret_store.get(HUGGINGFACE_SECRET),
            )
            try:
                diarization = analyze_session_speakers(
                    store,
                    session.id,
                    diarizer,
                    working_directory=self.paths.cache / "Diarization",
                    speaker_count=speaker_count,
                    cleanup_audio=True,
                )
                speaker_turns = len(diarization.result.turns)
            except (
                DiarizationAudioError,
                DiarizationError,
                DatabaseError,
                OSError,
                ValueError,
            ) as exc:
                warning = (
                    "逐字稿已完成，但說話者分析需稍後重試："
                    + " ".join(str(exc).split())[:400]
                )
                store.update_status(
                    session.id,
                    "transcript_ready",
                    duration_seconds=run.transcript.capture.captured_duration_seconds,
                    error_message=warning,
                )
        else:
            run.cleanup_chunks()

        pending_audio_files = sum(
            1 for path in spool_directory.glob("*.wav") if path.is_file()
        )
        stability = build_stability_report(
            session_id=session.id,
            transcript=run.transcript,
            observer=observer,
            pending_audio_files=pending_audio_files,
            cleanup_expected=warning is None,
        )
        saved_report = _write_json_atomic(report_path, stability)
        store.set_stability_report_path(session.id, saved_report)
        try:
            exported = export_session(
                store,
                session.id,
                output_root=Path(self.settings.export_directory),
            )
        except (DatabaseError, ExportError, OSError) as exc:
            try:
                store.update_status(
                    session.id,
                    "transcript_ready",
                    duration_seconds=run.transcript.capture.captured_duration_seconds,
                    error_message=(
                        "逐字稿已保存，但匯出尚未完成："
                        + " ".join(str(exc).split())[:400]
                    ),
                )
            except DatabaseError:
                pass
            raise RecordingWorkflowError(
                f"逐字稿已保存在 SQLite，但匯出失敗：{exc}"
            ) from exc
        if warning is None:
            store.update_status(
                session.id,
                "completed",
                duration_seconds=run.transcript.capture.captured_duration_seconds,
                error_message=None,
            )
        _emit(self.callbacks.on_status, "錄製與逐字稿處理完成。")
        return RecordingOutcome(
            session_id=session.id,
            captured_duration_seconds=run.transcript.capture.captured_duration_seconds,
            chunks_processed=run.transcript.chunks_processed,
            segments=len(run.transcript.segments),
            export=exported,
            speaker_turns=speaker_turns,
            warning=warning,
        )
