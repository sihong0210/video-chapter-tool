from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.database import SessionStore
from app.diarization.audio import DiarizationAudioError, assemble_session_wav
from app.diarization.models import DiarizationResult
from app.diarization.provider import DiarizationError, SpeakerDiarizer


@dataclass(frozen=True, slots=True)
class SessionDiarizationResult:
    session_id: int
    audio_path: Path
    result: DiarizationResult
    chunks_cleaned: int


def cleanup_retained_chunks(store: SessionStore, session_id: int) -> int:
    cleaned = 0
    directories: set[Path] = set()
    for record in store.list_session_chunks(session_id):
        path = Path(record.path).expanduser().resolve()
        directories.add(path.parent)
        if not path.exists():
            continue
        try:
            path.unlink()
            cleaned += 1
        except OSError as exc:
            raise DiarizationAudioError(f"無法清理已完成 Chunk：{path}；{exc}") from exc
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    return cleaned


def analyze_session_speakers(
    store: SessionStore,
    session_id: int,
    diarizer: SpeakerDiarizer,
    *,
    working_directory: Path,
    speaker_count: int | None = None,
    cleanup_audio: bool = True,
) -> SessionDiarizationResult:
    session = store.get_session(session_id)
    if not store.get_transcript_segments(session_id):
        raise DiarizationError("工作階段尚無可用逐字稿。")
    if store.list_pending_chunks(session_id):
        raise DiarizationError("仍有尚未完成的轉錄 Chunk，不能開始說話者分析。")
    chunks = store.list_session_chunks(session_id)
    if not chunks:
        raise DiarizationError("工作階段沒有登記過的音訊 Chunk。")
    audio_path = (
        working_directory.expanduser().resolve()
        / f"session_{session_id}_diarization.wav"
    )
    store.set_diarization_status(
        session_id,
        "analyzing",
        provider_name=diarizer.provider_name,
        model_name=diarizer.model_name,
        requested_speaker_count=speaker_count,
    )
    try:
        assemble_session_wav(chunks, audio_path, target_sample_rate=16_000)
        result = diarizer.analyze(audio_path, speaker_count=speaker_count)
        store.replace_speaker_turns(
            session_id,
            result.turns,
            provider_name=result.provider_name,
            model_name=result.model_name,
            requested_speaker_count=speaker_count,
        )
        cleaned = 0
        if cleanup_audio:
            audio_path.unlink(missing_ok=True)
            cleaned = cleanup_retained_chunks(store, session_id)
        return SessionDiarizationResult(
            session_id=session.id,
            audio_path=audio_path,
            result=result,
            chunks_cleaned=cleaned,
        )
    except Exception as exc:
        try:
            store.set_diarization_status(
                session_id,
                "failed",
                provider_name=diarizer.provider_name,
                model_name=diarizer.model_name,
                requested_speaker_count=speaker_count,
                error_message=" ".join(str(exc).split())[:500],
            )
        except Exception:
            pass
        if isinstance(exc, (DiarizationError, DiarizationAudioError)):
            raise
        raise DiarizationError(
            f"說話者分析失敗：{type(exc).__name__}: {exc}"
        ) from exc
