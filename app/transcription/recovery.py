from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.audio.chunking import AudioChunk
from app.database import SessionRecord, SessionStore
from app.transcription.merge import TranscriptMerger
from app.transcription.transcriber import BasicTranscriber, TranscriptSegment


class RecoveryError(RuntimeError):
    """Raised when durable pending audio cannot be recovered."""


class RecoveryCancelled(RecoveryError):
    """Raised between durable chunks when the user requests cancellation."""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    chunks_processed: int
    segments_added: int
    remaining_chunks: int
    segments: tuple[TranscriptSegment, ...]
    runtime: dict[str, object]


@dataclass(frozen=True, slots=True)
class RecoveryProgress:
    chunks_processed: int
    chunks_total: int
    segments_added: int


def _audio_chunk(record: object) -> AudioChunk:
    path = Path(getattr(record, "path")).expanduser().resolve()
    if not path.is_file():
        raise RecoveryError(f"找不到待復原音訊：{path}")
    try:
        with wave.open(str(path), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
    except (OSError, wave.Error) as exc:
        raise RecoveryError(f"待復原 WAV 無法讀取：{path}；{exc}") from exc
    return AudioChunk(
        index=int(getattr(record, "chunk_index")),
        path=path,
        start_frame=round(float(getattr(record, "start_time")) * sample_rate),
        end_frame=round(float(getattr(record, "end_time")) * sample_rate),
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )


def recover_session_chunks(
    store: SessionStore,
    session: SessionRecord,
    transcriber: BasicTranscriber,
    *,
    beam_size: int = 5,
    fallback_transcriber_factory: (
        Callable[[BaseException], BasicTranscriber | None] | None
    ) = None,
    normalize_text: Callable[[str], str] | None = None,
    retain_completed_audio: bool = False,
    word_timestamps: bool = False,
    on_progress: Callable[[RecoveryProgress], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RecoveryResult:
    existing_segments = store.get_transcript_segments(session.id)
    merger = TranscriptMerger(overlap_seconds=session.overlap_seconds)
    merger.seed(existing_segments)
    active_transcriber = transcriber
    fallback_attempted = False
    chunks_processed = 0
    segments_added = 0
    maximum_end = max(
        session.duration_seconds,
        store.get_recorded_timeline_end(session.id),
    )

    pending_records = store.list_pending_chunks(session.id)
    chunks_total = len(pending_records)
    store.update_status(session.id, "draining_queue")
    for pending_record in pending_records:
        if should_cancel is not None and should_cancel():
            raise RecoveryCancelled("工作階段復原已由使用者停止。")
        chunk = _audio_chunk(pending_record)
        try:
            result = active_transcriber.transcribe_file(
                chunk.path,
                language=session.language,
                beam_size=beam_size,
                vad_filter=True,
                word_timestamps=word_timestamps,
            )
        except Exception as exc:
            if fallback_transcriber_factory is None or fallback_attempted:
                store.mark_chunk_failed(session.id, chunk.index, str(exc))
                raise RecoveryError(
                    f"Chunk {chunk.index} 重新轉錄失敗：{exc}"
                ) from exc
            fallback_attempted = True
            fallback = fallback_transcriber_factory(exc)
            if fallback is None:
                store.mark_chunk_failed(session.id, chunk.index, str(exc))
                raise RecoveryError(
                    f"Chunk {chunk.index} 沒有可用的 CPU 備援。"
                ) from exc
            active_transcriber = fallback
            try:
                result = active_transcriber.transcribe_file(
                    chunk.path,
                    language=session.language,
                    beam_size=beam_size,
                    vad_filter=True,
                    word_timestamps=word_timestamps,
                )
            except Exception as fallback_exc:
                store.mark_chunk_failed(
                    session.id,
                    chunk.index,
                    str(fallback_exc),
                )
                raise RecoveryError(
                    f"Chunk {chunk.index} CPU 備援仍失敗：{fallback_exc}"
                ) from fallback_exc

        added = merger.add_chunk(
            chunk.start_seconds,
            result.segments,
            chunk_end_seconds=chunk.end_seconds,
        )
        store.commit_chunk_transcript(
            session.id,
            chunk,
            added,
            normalize_text=normalize_text,
        )
        if not retain_completed_audio:
            try:
                chunk.path.unlink(missing_ok=True)
            except OSError:
                # The SQLite transaction is already durable. A stale completed WAV
                # is safe to remove during later housekeeping.
                pass
        chunks_processed += 1
        segments_added += len(added)
        maximum_end = max(maximum_end, chunk.end_seconds)
        if on_progress is not None:
            try:
                on_progress(
                    RecoveryProgress(
                        chunks_processed=chunks_processed,
                        chunks_total=chunks_total,
                        segments_added=segments_added,
                    )
                )
            except Exception:
                # UI callbacks must not break durable recovery.
                pass

    runtime = active_transcriber.engine.runtime.to_dict()
    store.update_runtime(
        session.id,
        resolved_device=str(runtime["resolved_device"]),
        compute_type=str(runtime["compute_type"]),
    )
    remaining = len(store.list_pending_chunks(session.id))
    store.update_status(
        session.id,
        "transcript_ready",
        duration_seconds=maximum_end,
    )
    return RecoveryResult(
        chunks_processed=chunks_processed,
        segments_added=segments_added,
        remaining_chunks=remaining,
        segments=merger.segments,
        runtime=runtime,
    )
