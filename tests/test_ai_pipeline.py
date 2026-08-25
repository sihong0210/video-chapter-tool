from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app.ai import (
    AIAnalysisCancelled,
    AIAnalysisError,
    BatchAnalysis,
    ChapterDraft,
    LLMProvider,
    ProviderError,
    analyze_session,
    build_batches,
)
from app.ai.models import AIResponseValidationError, validate_batch_analysis
from app.audio.chunking import AudioChunk
from app.database import SessionStore
from app.exporting import export_summary
from app.transcription.transcriber import TranscriptSegment


class FakeProvider(LLMProvider):
    def __init__(self, failures: dict[int, int] | None = None) -> None:
        self.failures = dict(failures or {})
        self.batch_calls: Counter[int] = Counter()
        self.summary_calls = 0
        self.consolidation_calls = 0

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "deterministic-v1"

    def analyze_batch(self, batch, *, language: str) -> BatchAnalysis:
        self.batch_calls[batch.index] += 1
        if self.batch_calls[batch.index] <= self.failures.get(batch.index, 0):
            raise ProviderError(f"batch {batch.index} 暫時失敗")
        return BatchAnalysis(
            batch_summary=f"第 {batch.index + 1} 批摘要",
            chapters=(
                ChapterDraft(
                    start=batch.start,
                    end=batch.end,
                    title=f"主題 {batch.index + 1}",
                    summary=f"第 {batch.index + 1} 批的重點內容。",
                    key_points=("重點一", "重點二", "重點三"),
                ),
            ),
        )

    def summarize_video(
        self,
        *,
        title: str,
        chapters,
        batch_summaries,
        language: str,
    ) -> str:
        self.summary_calls += 1
        return f"{title} 共有 {len(chapters)} 個主題章節。"

    def consolidate_chapters(
        self,
        *,
        title: str,
        chapters,
        batch_summaries,
        language: str,
    ):
        self.consolidation_calls += 1
        return tuple(chapters)


class MergingProvider(FakeProvider):
    def consolidate_chapters(
        self,
        *,
        title: str,
        chapters,
        batch_summaries,
        language: str,
    ):
        self.consolidation_calls += 1
        return (
            ChapterDraft(
                start=chapters[0].start,
                end=chapters[-1].end,
                title="跨批次共同主題",
                summary="兩個文字批次實際上屬於同一個語意章節。",
                key_points=("跨批次辨識", "合併相鄰主題", "保留原始時間"),
            ),
        )


def create_transcript_session(root: Path) -> tuple[SessionStore, int]:
    store = SessionStore(root / "data.db")
    session = store.create_session(
        title="Stage 6 測試影片",
        platform=None,
        source=None,
        language="zh",
        model_name="small",
        requested_device="auto",
        export_directory=root / "exports",
    )
    chunk = AudioChunk(0, root / "chunk.wav", 0, 20, 1, 1, 2)
    chunk.path.write_bytes(b"audio")
    store.register_chunk(session.id, chunk)
    store.commit_chunk_transcript(
        session.id,
        chunk,
        (
            TranscriptSegment(0.0, 10.0, "第一個主題的內容。", -0.1, 0.01),
            TranscriptSegment(10.0, 20.0, "第二個主題的內容。", -0.1, 0.01),
        ),
    )
    store.update_status(session.id, "transcript_ready", duration_seconds=20.0)
    return store, session.id


class AIPipelineTests(unittest.TestCase):
    def test_batches_preserve_segment_boundaries_and_language_hash(self) -> None:
        segments = (
            TranscriptSegment(0, 10, "甲", 0, 0),
            TranscriptSegment(10, 20, "乙", 0, 0),
        )
        batches = build_batches(segments, max_seconds=10, language="zh-TW")
        other_language = build_batches(
            segments,
            max_seconds=10,
            language="en",
        )

        self.assertEqual([(batch.start, batch.end) for batch in batches], [(0, 10), (10, 20)])
        self.assertNotEqual(batches[0].request_hash, other_language[0].request_hash)

    def test_failed_batch_retries_and_completed_batches_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store, session_id = create_transcript_session(Path(temporary_directory))
            provider = FakeProvider({1: 1})

            first = analyze_session(
                store,
                session_id,
                provider,
                max_seconds=10,
                retry_attempts=2,
            )
            second = analyze_session(
                store,
                session_id,
                provider,
                max_seconds=10,
                retry_attempts=2,
            )

            self.assertEqual(provider.batch_calls, Counter({1: 2, 0: 1}))
            self.assertEqual(first.provider_calls, 5)
            self.assertEqual(second.batches_reused, 2)
            self.assertEqual(second.provider_calls, 2)
            self.assertEqual(store.get_session(session_id).status, "completed")
            self.assertEqual(len(store.get_chapters(session_id)), 2)

    def test_permanent_failure_preserves_transcript_and_successful_batch_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store, session_id = create_transcript_session(Path(temporary_directory))
            failing_provider = FakeProvider({1: 99})

            with self.assertRaises(AIAnalysisError):
                analyze_session(
                    store,
                    session_id,
                    failing_provider,
                    max_seconds=10,
                    retry_attempts=2,
                )

            self.assertEqual(len(store.get_transcript_segments(session_id)), 2)
            self.assertEqual(store.get_session(session_id).status, "transcript_ready")
            self.assertEqual(store.get_chapters(session_id), ())

            recovered_provider = FakeProvider()
            result = analyze_session(
                store,
                session_id,
                recovered_provider,
                max_seconds=10,
                retry_attempts=2,
            )
            self.assertEqual(result.batches_reused, 1)
            self.assertEqual(recovered_provider.batch_calls, Counter({1: 1}))

    def test_progress_reports_cache_and_cancel_preserves_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store, session_id = create_transcript_session(Path(temporary_directory))
            progress = []
            analyze_session(
                store,
                session_id,
                FakeProvider(),
                max_seconds=10,
            )

            with self.assertRaises(AIAnalysisCancelled):
                analyze_session(
                    store,
                    session_id,
                    FakeProvider(),
                    max_seconds=10,
                    on_progress=progress.append,
                    should_cancel=lambda: bool(progress),
                )

            self.assertEqual(progress[0].stage, "batches")
            self.assertEqual(store.get_session(session_id).status, "completed")
            self.assertEqual(len(store.get_transcript_segments(session_id)), 2)

    def test_summary_markdown_exports_saved_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store, session_id = create_transcript_session(Path(temporary_directory))
            analyze_session(store, session_id, FakeProvider(), max_seconds=10)

            summary_path = export_summary(store, session_id)
            content = summary_path.read_text(encoding="utf-8")

            self.assertEqual(summary_path.name, "summary.md")
            self.assertIn("# Stage 6 測試影片", content)
            self.assertIn("## 完整摘要", content)
            self.assertIn("### 00:00:00 主題 1", content)
            self.assertIn("- 重點三", content)

    def test_final_consolidation_can_merge_across_batch_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store, session_id = create_transcript_session(Path(temporary_directory))

            result = analyze_session(
                store,
                session_id,
                MergingProvider(),
                max_seconds=10,
            )

            self.assertEqual(len(result.chapters), 1)
            self.assertEqual(result.chapters[0].start, 0.0)
            self.assertEqual(result.chapters[0].end, 20.0)

    def test_rejects_overlapping_provider_chapters(self) -> None:
        batch = build_batches(
            (TranscriptSegment(0, 20, "測試", 0, 0),),
            max_seconds=30,
        )[0]
        invalid = BatchAnalysis(
            batch_summary="摘要",
            chapters=(
                ChapterDraft(0, 12, "一", "摘要一", ("甲", "乙", "丙")),
                ChapterDraft(10, 20, "二", "摘要二", ("甲", "乙", "丙")),
            ),
        )

        with self.assertRaises(AIResponseValidationError):
            validate_batch_analysis(batch, invalid)


if __name__ == "__main__":
    unittest.main()
