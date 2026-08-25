from __future__ import annotations

import gc
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

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
from app.infrastructure import CredentialError, HUGGINGFACE_SECRET, SecretStore
from app.models import ModelManager, ModelManagementError
from app.text_normalization import TextNormalizationError, build_text_normalizer
from app.transcription import (
    BasicTranscriber,
    EngineLoadError,
    RecoveryCancelled,
    RecoveryError,
    RecoveryProgress,
    RecoveryResult,
    WhisperEngine,
    recover_session_chunks,
)


class SessionRecoveryWorkflowError(RuntimeError):
    """Raised after recoverable session state has been preserved."""


@dataclass(frozen=True, slots=True)
class SessionRecoveryRequest:
    session_id: int


@dataclass(frozen=True, slots=True)
class SessionRecoveryOutcome:
    session_id: int
    recovery: RecoveryResult | None
    export: ExportResult | None
    speaker_turns: int = 0
    warning: str | None = None
    cancelled: bool = False


@dataclass(slots=True)
class SessionRecoveryCallbacks:
    on_status: Callable[[str], None] | None = None
    on_progress: Callable[[RecoveryProgress], None] | None = None


def _emit(callback: Callable[..., None] | None, *args: object) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        # UI feedback must never break durable recovery.
        pass


class SessionRecoveryWorkflow:
    def __init__(
        self,
        paths: AppPaths,
        settings: AppSettings,
        request: SessionRecoveryRequest,
        *,
        callbacks: SessionRecoveryCallbacks | None = None,
        secret_store: SecretStore | None = None,
        engine_loader: Callable[..., WhisperEngine] = WhisperEngine.load,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.request = request
        self.callbacks = callbacks or SessionRecoveryCallbacks()
        self.secret_store = secret_store or SecretStore()
        self.engine_loader = engine_loader
        self.stop_event = threading.Event()

    def request_cancel(self) -> None:
        self.stop_event.set()

    def _cancelled_outcome(self, store: SessionStore) -> SessionRecoveryOutcome:
        message = "工作階段復原已由使用者停止；已完成的 Chunk 仍保存在 SQLite。"
        store.update_status(
            self.request.session_id,
            "failed",
            error_message=message,
        )
        return SessionRecoveryOutcome(
            session_id=self.request.session_id,
            recovery=None,
            export=None,
            warning=message,
            cancelled=True,
        )

    @staticmethod
    def _cleanup_completed_audio(store: SessionStore, session_id: int) -> None:
        for record in store.list_session_chunks(session_id):
            if record.status != "completed":
                continue
            try:
                Path(record.path).expanduser().resolve().unlink(missing_ok=True)
            except OSError:
                pass

    def run(self) -> SessionRecoveryOutcome:
        store = SessionStore(self.paths.database_file)
        try:
            session = store.get_session(self.request.session_id)
            if session.status in {"completed", "cancelled"}:
                raise SessionRecoveryWorkflowError(
                    f"工作階段 #{session.id} 已經是完成狀態，不需要復原。"
                )
            pending = store.list_pending_chunks(session.id)
            recovery: RecoveryResult | None = None
            engine: WhisperEngine | None = None

            if pending:
                manager = ModelManager(self.settings.model_directory)
                model_status = manager.status(session.model_name)
                if not model_status.valid:
                    raise SessionRecoveryWorkflowError(
                        f"Whisper 模型 {session.model_name} 尚未完成下載或驗證。"
                    )
                _emit(self.callbacks.on_status, "正在載入 Whisper 模型…")
                engine = self.engine_loader(
                    manager.model_path(session.model_name),
                    requested_device=session.requested_device,
                )
                fallback_engine: WhisperEngine | None = None
                normalizer = build_text_normalizer(
                    language=session.language,
                    traditional_chinese_output=(
                        self.settings.traditional_chinese_output
                    ),
                )

                def build_cpu_fallback(error: BaseException) -> BasicTranscriber | None:
                    nonlocal fallback_engine
                    if engine is None or engine.runtime.resolved_device != "cuda":
                        return None
                    fallback_engine = self.engine_loader(
                        manager.model_path(session.model_name),
                        requested_device="cpu",
                    )
                    fallback_engine.runtime = replace(
                        fallback_engine.runtime,
                        requested_device=session.requested_device,
                        fallback_reason=(
                            "CUDA 復原推論失敗，已改用 CPU INT8："
                            + " ".join(str(error).split())[:240]
                        ),
                    )
                    return BasicTranscriber(fallback_engine)

                _emit(self.callbacks.on_status, "正在處理待復原音訊 Chunk…")
                try:
                    recovery = recover_session_chunks(
                        store,
                        session,
                        BasicTranscriber(engine),
                        fallback_transcriber_factory=build_cpu_fallback,
                        normalize_text=normalizer,
                        retain_completed_audio=(
                            session.diarization_status
                            in {"pending", "analyzing", "failed"}
                        ),
                        word_timestamps=(
                            session.diarization_status
                            in {"pending", "analyzing", "failed"}
                        ),
                        on_progress=lambda value: _emit(
                            self.callbacks.on_progress,
                            value,
                        ),
                        should_cancel=self.stop_event.is_set,
                    )
                finally:
                    engine.close()
                    if fallback_engine is not None:
                        fallback_engine.close()
            else:
                maximum_end = max(
                    session.duration_seconds,
                    store.get_recorded_timeline_end(session.id),
                )
                store.update_status(
                    session.id,
                    "transcript_ready",
                    duration_seconds=maximum_end,
                )
                recovery = RecoveryResult(
                    chunks_processed=0,
                    segments_added=0,
                    remaining_chunks=0,
                    segments=store.get_transcript_segments(session.id),
                    runtime={
                        "requested_device": session.requested_device,
                        "resolved_device": session.resolved_device,
                        "compute_type": session.compute_type,
                    },
                )

            if self.stop_event.is_set():
                return self._cancelled_outcome(store)

            refreshed = store.get_session(session.id)
            segments = store.get_transcript_segments(session.id)
            speaker_required = refreshed.diarization_status in {
                "pending",
                "analyzing",
                "failed",
            }
            warning: str | None = None
            speaker_turns = 0
            if speaker_required and segments:
                _emit(self.callbacks.on_status, "逐字稿已復原，正在分析說話者 A／B…")
                gc.collect()
                diarizer = PyannoteSpeakerDiarizer(
                    model_source=DEFAULT_PYANNOTE_MODEL,
                    cache_directory=(
                        Path(self.settings.model_directory) / "Diarization"
                    ),
                    requested_device=self.settings.diarization_device,
                    token=self.secret_store.get(HUGGINGFACE_SECRET),
                )
                try:
                    diarization = analyze_session_speakers(
                        store,
                        session.id,
                        diarizer,
                        working_directory=self.paths.cache / "Diarization",
                        speaker_count=refreshed.requested_speaker_count,
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
                        "逐字稿已復原，但說話者分析仍需稍後重試："
                        + " ".join(str(exc).split())[:400]
                    )
                    store.update_status(
                        session.id,
                        "transcript_ready",
                        error_message=warning,
                    )
            elif speaker_required:
                store.set_diarization_status(session.id, "skipped")
                self._cleanup_completed_audio(store, session.id)
            else:
                self._cleanup_completed_audio(store, session.id)

            _emit(self.callbacks.on_status, "正在重新匯出逐字稿…")
            exported = export_session(
                store,
                session.id,
                output_root=Path(self.settings.export_directory),
            )
            if warning is None:
                store.update_status(session.id, "completed")
            _emit(self.callbacks.on_status, "工作階段復原完成。")
            return SessionRecoveryOutcome(
                session_id=session.id,
                recovery=recovery,
                export=exported,
                speaker_turns=speaker_turns,
                warning=warning,
            )
        except RecoveryCancelled:
            return self._cancelled_outcome(store)
        except SessionRecoveryWorkflowError:
            raise
        except (
            DatabaseError,
            CredentialError,
            EngineLoadError,
            ExportError,
            ModelManagementError,
            RecoveryError,
            TextNormalizationError,
            OSError,
            ValueError,
        ) as exc:
            try:
                store.update_status(
                    self.request.session_id,
                    "failed",
                    error_message=" ".join(str(exc).split())[:500],
                )
            except DatabaseError:
                pass
            raise SessionRecoveryWorkflowError(
                f"工作階段復原失敗；SQLite 與待處理音訊已保留：{exc}"
            ) from exc
