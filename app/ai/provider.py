from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from app.ai.models import AnalysisBatch, BatchAnalysis, ChapterDraft


class ProviderError(RuntimeError):
    """Raised when an LLM provider request cannot produce a valid response."""


class LLMProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def analyze_batch(
        self,
        batch: AnalysisBatch,
        *,
        language: str,
    ) -> BatchAnalysis:
        raise NotImplementedError

    @abstractmethod
    def summarize_video(
        self,
        *,
        title: str,
        chapters: Sequence[ChapterDraft],
        batch_summaries: Sequence[str],
        language: str,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def consolidate_chapters(
        self,
        *,
        title: str,
        chapters: Sequence[ChapterDraft],
        batch_summaries: Sequence[str],
        language: str,
    ) -> Sequence[ChapterDraft]:
        """Merge adjacent cross-batch topics and preserve semantic splits."""
        raise NotImplementedError
