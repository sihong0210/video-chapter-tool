from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.ai import BatchAnalysis, ChapterDraft, LLMProvider
from app.audio.chunking import AudioChunk
from app.config import AppPaths
from app.database import SessionStore
from app.infrastructure import MemoryCredentialBackend, OPENAI_SECRET, SecretStore
from app.transcription.transcriber import TranscriptSegment
from app.workflows import AIAnalysisCallbacks, AIAnalysisRequest, AIAnalysisWorkflow


class _FakeProvider(LLMProvider):
    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    def analyze_batch(self, batch, *, language: str) -> BatchAnalysis:
        return BatchAnalysis(
            "批次摘要",
            (
                ChapterDraft(
                    batch.start,
                    batch.end,
                    "測試章節",
                    "章節摘要",
                    ("重點一", "重點二", "重點三"),
                ),
            ),
        )

    def consolidate_chapters(self, **options):
        return tuple(options["chapters"])

    def summarize_video(self, **_options) -> str:
        return "完整摘要"


def _create_session(paths: AppPaths) -> int:
    store = SessionStore(paths.database_file)
    session = store.create_session(
        title="GUI AI 測試",
        platform="test",
        source="test",
        language="zh",
        model_name="small",
        requested_device="cpu",
        export_directory=paths.exports,
    )
    chunk = AudioChunk(0, paths.pending_audio / "chunk.wav", 0, 20, 1, 1, 2)
    chunk.path.parent.mkdir(parents=True, exist_ok=True)
    chunk.path.write_bytes(b"audio")
    store.register_chunk(session.id, chunk)
    store.commit_chunk_transcript(
        session.id,
        chunk,
        (TranscriptSegment(0, 20, "可供 AI 分析的內容", -0.1, 0.01),),
    )
    store.update_status(session.id, "completed", duration_seconds=20)
    return session.id


class AIAnalysisWorkflowTests(unittest.TestCase):
    def test_workflow_uses_secret_reports_progress_and_exports_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            session_id = _create_session(paths)
            secrets = SecretStore(MemoryCredentialBackend())
            secrets.set(OPENAI_SECRET, "sk-test-value")
            progress = []
            workflow = AIAnalysisWorkflow(
                paths,
                AIAnalysisRequest(session_id),
                callbacks=AIAnalysisCallbacks(on_progress=progress.append),
                secret_store=secrets,
                provider_factory=lambda **_options: _FakeProvider(),
            )

            outcome = workflow.run()

            self.assertFalse(outcome.cancelled)
            self.assertIsNotNone(outcome.analysis)
            self.assertTrue(outcome.summary_path.is_file())
            self.assertEqual(progress[-1].stage, "completed")
            self.assertEqual(
                SessionStore(paths.database_file).get_session(session_id).ai_model,
                "fake-model",
            )


if __name__ == "__main__":
    unittest.main()
