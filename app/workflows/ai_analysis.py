from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.ai import (
    AIAnalysisCancelled,
    AIAnalysisError,
    AIAnalysisProgress,
    AnalysisResult,
    DEFAULT_OPENAI_MODEL,
    OpenAIProvider,
    ProviderError,
    analyze_session,
)
from app.config import AppPaths
from app.database import DatabaseError, SessionStore
from app.exporting import SummaryExportError, export_summary
from app.infrastructure import OPENAI_SECRET, CredentialError, SecretStore


class AIAnalysisWorkflowError(RuntimeError):
    """Raised after the durable transcript and successful batch cache are preserved."""


@dataclass(frozen=True, slots=True)
class AIAnalysisRequest:
    session_id: int
    model_name: str = DEFAULT_OPENAI_MODEL
    language: str = "zh-TW"


@dataclass(frozen=True, slots=True)
class AIAnalysisOutcome:
    session_id: int
    analysis: AnalysisResult | None
    summary_path: Path | None
    cancelled: bool = False


@dataclass(slots=True)
class AIAnalysisCallbacks:
    on_status: Callable[[str], None] | None = None
    on_progress: Callable[[AIAnalysisProgress], None] | None = None


def _emit(callback: Callable[..., None] | None, *args: object) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        pass


class AIAnalysisWorkflow:
    def __init__(
        self,
        paths: AppPaths,
        request: AIAnalysisRequest,
        *,
        callbacks: AIAnalysisCallbacks | None = None,
        secret_store: SecretStore | None = None,
        provider_factory: Callable[..., OpenAIProvider] = OpenAIProvider,
    ) -> None:
        self.paths = paths
        self.request = request
        self.callbacks = callbacks or AIAnalysisCallbacks()
        self.secret_store = secret_store or SecretStore()
        self.provider_factory = provider_factory
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> AIAnalysisOutcome:
        try:
            api_key = self.secret_store.get(OPENAI_SECRET)
        except CredentialError as exc:
            raise AIAnalysisWorkflowError(f"無法讀取 OpenAI API Key：{exc}") from exc
        if not api_key:
            raise AIAnalysisWorkflowError(
                "尚未設定 OpenAI API Key；請先到「錄製與儲存設定」保存。"
            )
        store = SessionStore(self.paths.database_file)
        session = store.get_session(self.request.session_id)
        if not store.get_transcript_segments(session.id):
            raise AIAnalysisWorkflowError("所選工作階段沒有可分析的逐字稿。")
        _emit(
            self.callbacks.on_status,
            "AI 分析已開始；只傳送逐字稿文字、說話者標籤與時間戳。",
        )
        try:
            provider = self.provider_factory(
                model_name=self.request.model_name,
                api_key=api_key,
            )
            analysis = analyze_session(
                store,
                session.id,
                provider,
                language=self.request.language,
                retry_attempts=3,
                retry_delay_seconds=2.0,
                on_progress=lambda progress: _emit(
                    self.callbacks.on_progress,
                    progress,
                ),
                should_cancel=self.cancel_event.is_set,
            )
            _emit(self.callbacks.on_status, "正在匯出 Markdown 摘要…")
            summary_path = export_summary(store, session.id)
        except AIAnalysisCancelled:
            return AIAnalysisOutcome(session.id, None, None, cancelled=True)
        except (
            AIAnalysisError,
            DatabaseError,
            ProviderError,
            SummaryExportError,
            OSError,
            ValueError,
        ) as exc:
            raise AIAnalysisWorkflowError(str(exc)) from exc
        _emit(self.callbacks.on_status, "AI 章節摘要完成。")
        return AIAnalysisOutcome(session.id, analysis, summary_path)
