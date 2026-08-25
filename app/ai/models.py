from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from app.transcription.transcriber import TranscriptSegment


class AIResponseValidationError(ValueError):
    """Raised when a provider response violates the chapter contract."""


@dataclass(frozen=True, slots=True)
class AnalysisBatch:
    index: int
    start: float
    end: float
    segments: tuple[TranscriptSegment, ...]
    prompt_text: str
    request_hash: str


@dataclass(frozen=True, slots=True)
class ChapterDraft:
    start: float
    end: float
    title: str
    summary: str
    key_points: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["key_points"] = list(self.key_points)
        return result

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChapterDraft":
        try:
            raw_points = data["key_points"]
            if not isinstance(raw_points, (list, tuple)):
                raise TypeError("key_points must be an array")
            return cls(
                start=round(float(data["start"]), 3),
                end=round(float(data["end"]), 3),
                title=str(data["title"]).strip(),
                summary=str(data["summary"]).strip(),
                key_points=tuple(str(point).strip() for point in raw_points),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIResponseValidationError(
                "AI 章節缺少必要欄位或欄位型別錯誤。"
            ) from exc


@dataclass(frozen=True, slots=True)
class BatchAnalysis:
    batch_summary: str
    chapters: tuple[ChapterDraft, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_summary": self.batch_summary,
            "chapters": [chapter.to_dict() for chapter in self.chapters],
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BatchAnalysis":
        raw_chapters = data.get("chapters")
        if not isinstance(raw_chapters, list):
            raise AIResponseValidationError("AI 回應的 chapters 必須是陣列。")
        return cls(
            batch_summary=str(data.get("batch_summary", "")).strip(),
            chapters=tuple(
                ChapterDraft.from_mapping(chapter)
                for chapter in raw_chapters
                if isinstance(chapter, Mapping)
            ),
        )


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    provider_name: str
    model_name: str
    overall_summary: str
    chapters: tuple[ChapterDraft, ...]
    batches_total: int
    batches_reused: int
    provider_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "overall_summary": self.overall_summary,
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "batches_total": self.batches_total,
            "batches_reused": self.batches_reused,
            "provider_calls": self.provider_calls,
        }


@dataclass(frozen=True, slots=True)
class AIAnalysisProgress:
    stage: str
    completed_batches: int
    total_batches: int
    reused_batches: int
    provider_calls: int
    message: str


def validate_batch_analysis(
    batch: AnalysisBatch,
    analysis: BatchAnalysis,
) -> BatchAnalysis:
    if not analysis.batch_summary:
        raise AIResponseValidationError("AI 批次摘要不可為空。")
    if not analysis.chapters:
        raise AIResponseValidationError("AI 批次至少需要一個章節。")

    previous_end = batch.start
    validated: list[ChapterDraft] = []
    for chapter in analysis.chapters:
        if not chapter.title:
            raise AIResponseValidationError("AI 章節標題不可為空。")
        if not chapter.summary:
            raise AIResponseValidationError("AI 章節摘要不可為空。")
        if not 3 <= len(chapter.key_points) <= 5:
            raise AIResponseValidationError("每章重點必須介於 3～5 項。")
        if any(not point for point in chapter.key_points):
            raise AIResponseValidationError("章節重點不可包含空字串。")
        if chapter.start < batch.start - 0.05 or chapter.end > batch.end + 0.05:
            raise AIResponseValidationError("AI 章節時間超出目前批次範圍。")
        if chapter.end <= chapter.start:
            raise AIResponseValidationError("AI 章節結束時間必須晚於開始時間。")
        if chapter.start < previous_end - 0.05:
            raise AIResponseValidationError("AI 章節時間不可倒序或互相重疊。")
        previous_end = chapter.end
        validated.append(chapter)
    return BatchAnalysis(analysis.batch_summary, tuple(validated))


def validate_final_chapters(
    chapters: Iterable[ChapterDraft],
    *,
    timeline_end: float,
) -> tuple[ChapterDraft, ...]:
    try:
        values = tuple(chapters)
    except TypeError as exc:
        raise AIResponseValidationError("最終章節必須是可迭代的章節陣列。") from exc
    if not values:
        raise AIResponseValidationError("AI 分析沒有產生任何章節。")
    previous_end = 0.0
    for chapter in values:
        if not isinstance(chapter, ChapterDraft):
            raise AIResponseValidationError("最終章節格式不正確。")
        if not chapter.title or not chapter.summary:
            raise AIResponseValidationError("最終章節的標題與摘要不可為空。")
        if not 3 <= len(chapter.key_points) <= 5:
            raise AIResponseValidationError("最終章節必須包含 3～5 個重點。")
        if any(not point for point in chapter.key_points):
            raise AIResponseValidationError("最終章節重點不可包含空字串。")
        if chapter.end <= chapter.start:
            raise AIResponseValidationError("最終章節結束時間必須晚於開始時間。")
        if chapter.start < -0.05:
            raise AIResponseValidationError("最終章節開始時間不可小於零。")
        if chapter.start < previous_end - 0.05:
            raise AIResponseValidationError("合併後章節倒序或重疊。")
        if chapter.end > timeline_end + 0.05:
            raise AIResponseValidationError("合併後章節超出逐字稿範圍。")
        previous_end = chapter.end
    return values
