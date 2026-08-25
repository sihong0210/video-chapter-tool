from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from app.ai.models import AnalysisBatch, BatchAnalysis, ChapterDraft
from app.ai.provider import LLMProvider, ProviderError


DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"


def _chapter_schema(*, minimum: float | None = None, maximum: float | None = None) -> dict[str, Any]:
    start_schema: dict[str, Any] = {"type": "number"}
    end_schema: dict[str, Any] = {"type": "number"}
    if minimum is not None:
        start_schema["minimum"] = minimum
        end_schema["minimum"] = minimum
    if maximum is not None:
        start_schema["maximum"] = maximum
        end_schema["maximum"] = maximum
    return {
        "type": "object",
        "properties": {
            "start": start_schema,
            "end": end_schema,
            "title": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "key_points": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 3,
                "maxItems": 5,
            },
        },
        "required": ["start", "end", "title", "summary", "key_points"],
        "additionalProperties": False,
    }


class OpenAIProvider(LLMProvider):
    """Responses API adapter that sends transcript text, never source audio."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_OPENAI_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._model_name = model_name.strip()
        if not self._model_name:
            raise ValueError("OpenAI 模型名稱不可為空。")
        if client is not None:
            self._client = client
            return
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ProviderError(
                "找不到 OPENAI_API_KEY；請先在目前 PowerShell 工作階段設定環境變數。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError(
                "尚未安裝 OpenAI Python SDK，請重新安裝 requirements.lock。"
            ) from exc
        self._client = OpenAI(
            api_key=resolved_key,
            max_retries=0,
            timeout=90.0,
        )

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model_name

    def _request_json(
        self,
        *,
        schema_name: str,
        schema: Mapping[str, Any],
        instructions: str,
        input_text: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        try:
            response = self._client.responses.create(
                model=self.model_name,
                instructions=instructions,
                input=input_text,
                reasoning={"effort": "low"},
                text={
                    "verbosity": "low",
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": dict(schema),
                    },
                },
                max_output_tokens=max_output_tokens,
                store=False,
            )
            raw_text = str(response.output_text or "").strip()
            if not raw_text:
                raise ProviderError("OpenAI 沒有回傳可解析的文字內容。")
            payload = json.loads(raw_text)
            if not isinstance(payload, dict):
                raise ProviderError("OpenAI 結構化回應必須是 JSON 物件。")
            return payload
        except ProviderError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"OpenAI 回應解析失敗：{exc}") from exc
        except Exception as exc:
            message = " ".join(str(exc).split())[:500] or type(exc).__name__
            raise ProviderError(f"OpenAI API 呼叫失敗：{message}") from exc

    def analyze_batch(
        self,
        batch: AnalysisBatch,
        *,
        language: str,
    ) -> BatchAnalysis:
        schema = {
            "type": "object",
            "properties": {
                "batch_summary": {"type": "string", "minLength": 1},
                "chapters": {
                    "type": "array",
                    "items": _chapter_schema(
                        minimum=max(0.0, batch.start),
                        maximum=batch.end,
                    ),
                    "minItems": 1,
                },
            },
            "required": ["batch_summary", "chapters"],
            "additionalProperties": False,
        }
        instructions = f"""
你是長影片逐字稿的主題章節編輯。請以 {language} 輸出。
只根據提供的逐字稿與時間戳整理，不補充未出現的事實。
主題明顯改變才建立新章節；零碎或太短且無獨立意義的內容要與相鄰主題合併。
過長且明顯含多個子主題時可拆分。不要按固定分鐘數切章。
章節必須依時間排序、不可重疊，且 start/end 必須落在本批範圍內。
每章提供清楚標題、精簡摘要與 3～5 個具體重點。
""".strip()
        payload = self._request_json(
            schema_name="transcript_batch_analysis",
            schema=schema,
            instructions=instructions,
            input_text=batch.prompt_text,
            max_output_tokens=6_000,
        )
        return BatchAnalysis.from_mapping(payload)

    def consolidate_chapters(
        self,
        *,
        title: str,
        chapters: Sequence[ChapterDraft],
        batch_summaries: Sequence[str],
        language: str,
    ) -> Sequence[ChapterDraft]:
        schema = {
            "type": "object",
            "properties": {
                "chapters": {
                    "type": "array",
                    "items": _chapter_schema(minimum=0.0),
                    "minItems": 1,
                }
            },
            "required": ["chapters"],
            "additionalProperties": False,
        }
        input_payload = {
            "title": title,
            "batch_summaries": list(batch_summaries),
            "draft_chapters": [chapter.to_dict() for chapter in chapters],
        }
        instructions = f"""
你是長影片的最終章節編輯。請以 {language} 輸出。
檢查相鄰初稿章節；若只是因文字分批而切開但語意相同，請合併並整合摘要與重點。
保留真正的主題轉換，不按固定時間合併。不得新增初稿沒有的事實。
只能使用初稿既有的 start 或 end 作為最終邊界，不得自行發明時間點。
最終章節依時間排序、不可重疊，每章需有 3～5 個重點。
""".strip()
        payload = self._request_json(
            schema_name="consolidated_video_chapters",
            schema=schema,
            instructions=instructions,
            input_text=json.dumps(input_payload, ensure_ascii=False),
            max_output_tokens=8_000,
        )
        raw_chapters = payload.get("chapters")
        if not isinstance(raw_chapters, list):
            raise ProviderError("OpenAI 最終章節回應缺少 chapters 陣列。")
        consolidated = tuple(
            ChapterDraft.from_mapping(value)
            for value in raw_chapters
            if isinstance(value, Mapping)
        )
        allowed_starts = tuple(chapter.start for chapter in chapters)
        allowed_ends = tuple(chapter.end for chapter in chapters)
        for chapter in consolidated:
            if not any(abs(chapter.start - value) <= 0.05 for value in allowed_starts):
                raise ProviderError("OpenAI 最終章節使用了初稿不存在的開始時間。")
            if not any(abs(chapter.end - value) <= 0.05 for value in allowed_ends):
                raise ProviderError("OpenAI 最終章節使用了初稿不存在的結束時間。")
        return consolidated

    def summarize_video(
        self,
        *,
        title: str,
        chapters: Sequence[ChapterDraft],
        batch_summaries: Sequence[str],
        language: str,
    ) -> str:
        schema = {
            "type": "object",
            "properties": {
                "overall_summary": {"type": "string", "minLength": 1}
            },
            "required": ["overall_summary"],
            "additionalProperties": False,
        }
        input_payload = {
            "title": title,
            "batch_summaries": list(batch_summaries),
            "chapters": [chapter.to_dict() for chapter in chapters],
        }
        payload = self._request_json(
            schema_name="video_overall_summary",
            schema=schema,
            instructions=(
                f"請以 {language} 根據提供的章節資料撰寫一段完整但精簡的全片摘要。"
                "不得補充資料中沒有的事實，也不要重複列出所有章節標題。"
            ),
            input_text=json.dumps(input_payload, ensure_ascii=False),
            max_output_tokens=1_500,
        )
        return str(payload.get("overall_summary", "")).strip()
