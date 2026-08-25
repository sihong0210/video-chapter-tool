from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.ai import ChapterDraft
from app.ai.openai_provider import OpenAIProvider
from app.ai.pipeline import build_batches
from app.transcription.transcriber import TranscriptSegment


class FakeResponses:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        return SimpleNamespace(output_text=json.dumps(payload, ensure_ascii=False))


class FakeOpenAIClient:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.responses = FakeResponses(payloads)


class OpenAIProviderTests(unittest.TestCase):
    def test_uses_structured_outputs_without_storing_response(self) -> None:
        client = FakeOpenAIClient(
            [
                {
                    "batch_summary": "本批摘要",
                    "chapters": [
                        {
                            "start": 0,
                            "end": 10,
                            "title": "產品介紹",
                            "summary": "說明產品特色。",
                            "key_points": ["特色一", "特色二", "特色三"],
                        }
                    ],
                }
            ]
        )
        provider = OpenAIProvider(client=client)
        batch = build_batches(
            (TranscriptSegment(0, 10, "介紹產品特色", 0, 0),)
        )[0]

        analysis = provider.analyze_batch(batch, language="zh-TW")

        call = client.responses.calls[0]
        self.assertEqual(analysis.chapters[0].title, "產品介紹")
        self.assertFalse(call["store"])
        self.assertEqual(call["model"], "gpt-5.6-luna")
        self.assertEqual(call["text"]["format"]["type"], "json_schema")
        self.assertTrue(call["text"]["format"]["strict"])

    def test_consolidation_and_summary_are_parsed(self) -> None:
        client = FakeOpenAIClient(
            [
                {
                    "chapters": [
                        {
                            "start": 0,
                            "end": 20,
                            "title": "合併主題",
                            "summary": "相鄰內容屬於同一主題。",
                            "key_points": ["甲", "乙", "丙"],
                        }
                    ]
                },
                {"overall_summary": "這是一段全片摘要。"},
            ]
        )
        provider = OpenAIProvider(client=client)
        drafts = (
            ChapterDraft(0, 10, "主題", "前半", ("甲", "乙", "丙")),
            ChapterDraft(10, 20, "主題", "後半", ("甲", "乙", "丙")),
        )

        chapters = provider.consolidate_chapters(
            title="測試",
            chapters=drafts,
            batch_summaries=("摘要",),
            language="zh-TW",
        )
        summary = provider.summarize_video(
            title="測試",
            chapters=chapters,
            batch_summaries=("摘要",),
            language="zh-TW",
        )

        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].end, 20)
        self.assertEqual(summary, "這是一段全片摘要。")


if __name__ == "__main__":
    unittest.main()
