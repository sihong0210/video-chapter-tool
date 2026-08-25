from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TypeVar

from app.ai.models import (
    AIAnalysisProgress,
    AIResponseValidationError,
    AnalysisBatch,
    AnalysisResult,
    BatchAnalysis,
    ChapterDraft,
    validate_batch_analysis,
    validate_final_chapters,
)
from app.ai.provider import LLMProvider, ProviderError
from app.database import DatabaseError, SessionStore
from app.diarization import SpeakerTurn, label_transcript_segments
from app.transcription.transcriber import TranscriptSegment


class AIAnalysisError(RuntimeError):
    """Raised when chapter analysis cannot be completed safely."""


class AIAnalysisCancelled(AIAnalysisError):
    """Raised between provider requests when the user asks to stop analysis."""


T = TypeVar("T")


def _emit_progress(
    callback: Callable[[AIAnalysisProgress], None] | None,
    progress: AIAnalysisProgress,
) -> None:
    if callback is None:
        return
    try:
        callback(progress)
    except Exception:
        pass


def _prompt_line(segment: TranscriptSegment) -> str:
    speaker = f" [{segment.speaker_id}]" if segment.speaker_id else ""
    return (
        f"[{segment.start:.1f}-{segment.end:.1f}]{speaker} "
        f"{segment.text.strip()}"
    )


def _make_batch(
    index: int,
    segments: list[TranscriptSegment],
    *,
    language: str,
) -> AnalysisBatch:
    prompt_text = "\n".join(_prompt_line(segment) for segment in segments)
    hash_payload = {
        "contract": 1,
        "language": language,
        "segments": [
            {
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text.strip(),
            }
            for segment in segments
        ],
    }
    request_hash = hashlib.sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AnalysisBatch(
        index=index,
        start=segments[0].start,
        end=segments[-1].end,
        segments=tuple(segments),
        prompt_text=prompt_text,
        request_hash=request_hash,
    )


def build_batches(
    segments: Iterable[TranscriptSegment],
    *,
    max_chars: int = 12_000,
    max_seconds: float = 900.0,
    language: str = "zh-TW",
) -> tuple[AnalysisBatch, ...]:
    """Split transcripts on segment boundaries without inventing timestamps."""
    if max_chars <= 0:
        raise ValueError("max_chars 必須大於 0。")
    if max_seconds <= 0:
        raise ValueError("max_seconds 必須大於 0。")

    batches: list[AnalysisBatch] = []
    current: list[TranscriptSegment] = []
    current_chars = 0
    for segment in segments:
        if segment.end <= segment.start or not segment.text.strip():
            continue
        line_chars = len(_prompt_line(segment)) + (1 if current else 0)
        exceeds_chars = bool(current) and current_chars + line_chars > max_chars
        exceeds_time = bool(current) and segment.end - current[0].start > max_seconds
        if exceeds_chars or exceeds_time:
            batches.append(
                _make_batch(len(batches), current, language=language)
            )
            current = []
            current_chars = 0
            line_chars = len(_prompt_line(segment))
        current.append(segment)
        current_chars += line_chars
    if current:
        batches.append(_make_batch(len(batches), current, language=language))
    return tuple(batches)


def _retry(
    operation: Callable[[], T],
    *,
    attempts: int,
    delay_seconds: float,
    on_failure: Callable[[int, Exception], None] | None = None,
) -> tuple[T, int]:
    if attempts <= 0:
        raise ValueError("retry_attempts 必須大於 0。")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation(), attempt
        except (ProviderError, AIResponseValidationError) as exc:
            last_error = exc
            if on_failure is not None:
                on_failure(attempt, exc)
            if attempt < attempts and delay_seconds > 0:
                time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def analyze_session(
    store: SessionStore,
    session_id: int,
    provider: LLMProvider,
    *,
    language: str = "zh-TW",
    max_chars: int = 12_000,
    max_seconds: float = 900.0,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 0.0,
    on_progress: Callable[[AIAnalysisProgress], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> AnalysisResult:
    """Analyze one durable transcript, caching each successful provider batch."""
    session = store.get_session(session_id)
    segments = store.get_transcript_segments(session_id)
    speaker_turns = tuple(
        SpeakerTurn(
            start=row.start_time,
            end=row.end_time,
            speaker_id=row.speaker_id,
        )
        for row in store.get_speaker_turns(session_id)
    )
    if speaker_turns:
        labels = {
            row.speaker_id: row.display_name
            for row in store.get_speaker_labels(session_id)
        }
        labeled = label_transcript_segments(
            segments,
            speaker_turns,
            labels=labels,
        )
        segments = tuple(
            replace(
                segment,
                speaker_id=(item.speaker_name or item.speaker_id),
            )
            for segment, item in zip(segments, labeled, strict=True)
        )
    if not segments:
        raise AIAnalysisError("工作階段沒有可分析的逐字稿。")
    batches = build_batches(
        segments,
        max_chars=max_chars,
        max_seconds=max_seconds,
        language=language,
    )
    if not batches:
        raise AIAnalysisError("逐字稿沒有可分析的文字片段。")

    store.update_status(session_id, "ai_analyzing")
    reused = 0
    provider_calls = 0
    analyses: list[BatchAnalysis] = []
    fallback_status = "completed" if session.overall_summary else "transcript_ready"
    try:
        _emit_progress(
            on_progress,
            AIAnalysisProgress(
                "batches",
                0,
                len(batches),
                reused,
                provider_calls,
                f"準備分析 {len(batches)} 個文字批次。",
            ),
        )
        for batch in batches:
            if should_cancel is not None and should_cancel():
                raise AIAnalysisCancelled("AI 分析已由使用者停止。")
            cached = store.get_cached_ai_batch(
                session_id,
                batch_index=batch.index,
                provider_name=provider.provider_name,
                model_name=provider.model_name,
                request_hash=batch.request_hash,
            )
            if cached is not None:
                try:
                    cached_analysis = validate_batch_analysis(
                        batch,
                        BatchAnalysis.from_mapping(cached),
                    )
                except AIResponseValidationError:
                    cached_analysis = None
                if cached_analysis is not None:
                    analyses.append(cached_analysis)
                    reused += 1
                    _emit_progress(
                        on_progress,
                        AIAnalysisProgress(
                            "batches",
                            len(analyses),
                            len(batches),
                            reused,
                            provider_calls,
                            f"沿用第 {batch.index + 1} 批成功快取。",
                        ),
                    )
                    continue

            def call_provider() -> BatchAnalysis:
                nonlocal provider_calls
                provider_calls += 1
                return validate_batch_analysis(
                    batch,
                    provider.analyze_batch(batch, language=language),
                )

            def record_failure(attempt: int, error: Exception) -> None:
                store.save_ai_batch_failure(
                    session_id,
                    batch_index=batch.index,
                    start_time=batch.start,
                    end_time=batch.end,
                    provider_name=provider.provider_name,
                    model_name=provider.model_name,
                    request_hash=batch.request_hash,
                    attempts=attempt,
                    error_message=str(error),
                )

            analysis, attempts_used = _retry(
                call_provider,
                attempts=retry_attempts,
                delay_seconds=retry_delay_seconds,
                on_failure=record_failure,
            )
            store.save_ai_batch_success(
                session_id,
                batch_index=batch.index,
                start_time=batch.start,
                end_time=batch.end,
                provider_name=provider.provider_name,
                model_name=provider.model_name,
                request_hash=batch.request_hash,
                attempts=attempts_used,
                response=analysis.to_dict(),
            )
            analyses.append(analysis)
            _emit_progress(
                on_progress,
                AIAnalysisProgress(
                    "batches",
                    len(analyses),
                    len(batches),
                    reused,
                    provider_calls,
                    f"第 {batch.index + 1} 批分析完成。",
                ),
            )

        timeline_end = store.get_recorded_timeline_end(session_id)
        draft_chapters = validate_final_chapters(
            (chapter for analysis in analyses for chapter in analysis.chapters),
            timeline_end=timeline_end,
        )

        if should_cancel is not None and should_cancel():
            raise AIAnalysisCancelled("AI 分析已由使用者停止。")
        _emit_progress(
            on_progress,
            AIAnalysisProgress(
                "consolidating",
                len(analyses),
                len(batches),
                reused,
                provider_calls,
                "正在合併跨批次章節。",
            ),
        )

        def call_consolidation() -> tuple[ChapterDraft, ...]:
            nonlocal provider_calls
            provider_calls += 1
            return validate_final_chapters(
                provider.consolidate_chapters(
                    title=session.title,
                    chapters=draft_chapters,
                    batch_summaries=tuple(
                        analysis.batch_summary for analysis in analyses
                    ),
                    language=language,
                ),
                timeline_end=timeline_end,
            )

        chapters, _ = _retry(
            call_consolidation,
            attempts=retry_attempts,
            delay_seconds=retry_delay_seconds,
        )

        if should_cancel is not None and should_cancel():
            raise AIAnalysisCancelled("AI 分析已由使用者停止。")
        _emit_progress(
            on_progress,
            AIAnalysisProgress(
                "summarizing",
                len(analyses),
                len(batches),
                reused,
                provider_calls,
                "正在撰寫完整摘要。",
            ),
        )

        def call_summary() -> str:
            nonlocal provider_calls
            provider_calls += 1
            value = provider.summarize_video(
                title=session.title,
                chapters=chapters,
                batch_summaries=tuple(
                    analysis.batch_summary for analysis in analyses
                ),
                language=language,
            ).strip()
            if not value:
                raise AIResponseValidationError("AI 回傳的完整摘要不可為空。")
            return value

        overall_summary, _ = _retry(
            call_summary,
            attempts=retry_attempts,
            delay_seconds=retry_delay_seconds,
        )
        store.replace_ai_result(
            session_id,
            overall_summary=overall_summary,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            chapters=(chapter.to_dict() for chapter in chapters),
        )
        _emit_progress(
            on_progress,
            AIAnalysisProgress(
                "completed",
                len(analyses),
                len(batches),
                reused,
                provider_calls,
                "AI 章節與摘要已保存。",
            ),
        )
        return AnalysisResult(
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            overall_summary=overall_summary,
            chapters=chapters,
            batches_total=len(batches),
            batches_reused=reused,
            provider_calls=provider_calls,
        )
    except AIAnalysisCancelled:
        store.update_status(
            session_id,
            fallback_status,
            error_message="AI 分析已由使用者停止。",
        )
        raise
    except (AIResponseValidationError, DatabaseError, ProviderError) as exc:
        store.update_status(session_id, fallback_status, error_message=str(exc))
        raise AIAnalysisError(f"AI 章節分析失敗：{exc}") from exc
