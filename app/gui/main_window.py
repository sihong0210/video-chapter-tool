from __future__ import annotations

import json
import logging
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThreadPool, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.ai import DEFAULT_OPENAI_MODEL
from app.config import AppPaths, AppSettings, SettingsError, SettingsStore
from app.database import DatabaseError, SessionStore
from app.diagnostics import collect_diagnostics
from app.exporting import ExportResult, export_session
from app.gui.workers import (
    AIAnalysisSignals,
    FunctionWorker,
    MaintenanceSignals,
    RecordingSignals,
)
from app.infrastructure import (
    CredentialError,
    HUGGINGFACE_SECRET,
    OPENAI_SECRET,
    SecretStore,
)
from app.models import ModelManager, ModelManagementError, ModelStatus
from app.transcription import RecoveryProgress
from app.workflows import (
    AIAnalysisCallbacks,
    AIAnalysisOutcome,
    AIAnalysisRequest,
    AIAnalysisWorkflow,
    GUI_SAFETY_DURATION_SECONDS,
    RecordingCallbacks,
    RecordingOutcome,
    RecordingRequest,
    RecordingWorkflow,
    SessionRecoveryCallbacks,
    SessionRecoveryOutcome,
    SessionRecoveryRequest,
    SessionRecoveryWorkflow,
)


_STATUS_LABELS = {
    "created": "已建立",
    "capturing": "擷取中",
    "draining_queue": "處理剩餘音訊",
    "transcript_ready": "逐字稿完成",
    "ai_analyzing": "AI 分析中",
    "diarization_pending": "等待說話者分析",
    "diarizing": "說話者分析中",
    "completed": "完成",
    "failed": "需要處理",
    "cancelled": "已停止",
}


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _gib_text(value: Any) -> str:
    try:
        return f"{float(value):.1f} GB"
    except (TypeError, ValueError):
        return "未知"


def _file_size_text(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "—"
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.2f} GiB"
    return f"{size_bytes / 1024**2:.1f} MiB"


class _InfoCard(QFrame):
    def __init__(self, title: str, value: str, detail: str = "") -> None:
        super().__init__()
        self.setObjectName("infoCard")
        layout = QVBoxLayout(self)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("cardValue")
        self.value_label.setWordWrap(True)
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("cardDetail")
        self.detail_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)
        layout.addStretch(1)

    def set_content(self, value: str, detail: str = "") -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)


class MainWindow(QMainWindow):
    def __init__(
        self,
        paths: AppPaths,
        *,
        auto_refresh: bool = True,
        secret_store: SecretStore | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.logger = logging.getLogger("video_chapter_tool")
        self._last_diagnostics_report: dict[str, Any] | None = None
        self.paths.ensure()
        self.settings_store = SettingsStore(paths)
        self.settings = self.settings_store.load_or_create()
        self.session_store = SessionStore(paths.database_file)
        self.secret_store = secret_store or SecretStore()
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[FunctionWorker] = set()
        self._recording_worker: FunctionWorker | None = None
        self._recording_workflow: RecordingWorkflow | None = None
        self._recording_signals: RecordingSignals | None = None
        self._recording_output_directory: Path | None = None
        self._close_after_recording = False
        self._ai_worker: FunctionWorker | None = None
        self._ai_workflow: AIAnalysisWorkflow | None = None
        self._ai_signals: AIAnalysisSignals | None = None
        self._ai_summary_path: Path | None = None
        self._ai_output_directory: Path | None = None
        self._close_after_ai = False
        self._maintenance_worker: FunctionWorker | None = None
        self._maintenance_signals: MaintenanceSignals | None = None
        self._maintenance_task: str | None = None
        self._recovery_workflow: SessionRecoveryWorkflow | None = None
        self._recovery_output_directory: Path | None = None
        self._close_after_maintenance = False
        self._model_statuses: dict[str, ModelStatus] = {}
        self._session_action_worker: FunctionWorker | None = None
        self._session_action_session_id: int | None = None

        self.setWindowTitle("直播影片摘要工具")
        self.setMinimumSize(1040, 700)
        self.resize(1240, 820)
        self._build_ui()
        self._apply_style()
        self._load_settings_into_form()
        self.refresh_sessions()
        self.refresh_ai_sessions()
        self.refresh_model_statuses()
        self.refresh_recovery_sessions()
        self._refresh_secret_status()
        if auto_refresh:
            self.refresh_diagnostics()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(30, 24, 30, 18)
        root.setSpacing(16)

        heading_row = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("直播影片摘要工具")
        title.setObjectName("pageTitle")
        subtitle = QLabel("系統音訊轉錄、多人標註與 AI 章節摘要")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        heading_row.addLayout(heading)
        heading_row.addStretch(1)
        release_channel = "Portable" if getattr(sys, "frozen", False) else "開發版"
        version = QLabel(f"{release_channel} · v{__version__}")
        version.setObjectName("versionBadge")
        heading_row.addWidget(version, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(heading_row)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.addTab(self._build_overview_tab(), "首頁")
        self.recording_tab_index = self.tabs.addTab(
            self._build_recording_tab(), "錄製"
        )
        self.ai_tab_index = self.tabs.addTab(
            self._build_ai_tab(), "AI 摘要"
        )
        self.maintenance_tab_index = self.tabs.addTab(
            self._build_maintenance_tab(), "模型與復原"
        )
        self.settings_tab_index = self.tabs.addTab(
            self._build_settings_tab(), "設定"
        )
        self.tabs.addTab(self._build_diagnostics_tab(), "診斷")
        root.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("就緒")

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 18, 0, 0)
        layout.setSpacing(16)

        cards = QGridLayout()
        cards.setSpacing(14)
        for column in range(3):
            cards.setColumnStretch(column, 1)
        self.hardware_card = _InfoCard("本機運算", "檢測中…", "")
        self.audio_card = _InfoCard("系統音訊", "檢測中…", "WASAPI Loopback")
        self.cloud_card = _InfoCard("AI 與模型授權", "檢查中…", "不會顯示或保存金鑰")
        cards.addWidget(self.hardware_card, 0, 0)
        cards.addWidget(self.audio_card, 0, 1)
        cards.addWidget(self.cloud_card, 0, 2)
        layout.addLayout(cards)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("重新檢測")
        self.refresh_button.clicked.connect(self.refresh_diagnostics)
        self.start_button = QPushButton("開始新的錄製")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._show_recording_tab)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        actions.addWidget(self.start_button)
        layout.addLayout(actions)

        section_row = QHBoxLayout()
        section_title = QLabel("最近工作階段")
        section_title.setObjectName("sectionTitle")
        section_row.addWidget(section_title)
        section_row.addStretch(1)
        refresh_sessions = QPushButton("重新整理")
        refresh_sessions.clicked.connect(self.refresh_sessions)
        section_row.addWidget(refresh_sessions)
        layout.addLayout(section_row)

        self.sessions_table = QTableWidget(0, 5)
        self.sessions_table.setObjectName("sessionsTable")
        self.sessions_table.setHorizontalHeaderLabels(
            ["編號", "標題", "狀態", "長度", "運算"]
        )
        self.sessions_table.verticalHeader().setVisible(False)
        self.sessions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sessions_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.sessions_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.sessions_table.setAlternatingRowColors(True)
        self.sessions_table.setShowGrid(False)
        self.sessions_table.itemSelectionChanged.connect(
            self._update_session_action_state
        )
        header = self.sessions_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.sessions_table, 1)

        session_actions = QHBoxLayout()
        self.session_action_hint = QLabel("選取工作階段後可開啟、重新匯出或刪除。")
        self.session_action_hint.setObjectName("mutedText")
        self.session_open_button = QPushButton("開啟輸出")
        self.session_open_button.setEnabled(False)
        self.session_open_button.clicked.connect(self._open_selected_session_output)
        self.session_reexport_button = QPushButton("重新匯出")
        self.session_reexport_button.setEnabled(False)
        self.session_reexport_button.clicked.connect(self._reexport_selected_session)
        self.session_delete_button = QPushButton("刪除工作階段")
        self.session_delete_button.setObjectName("dangerButton")
        self.session_delete_button.setEnabled(False)
        self.session_delete_button.clicked.connect(self._delete_selected_session)
        session_actions.addWidget(self.session_action_hint, 1)
        session_actions.addWidget(self.session_open_button)
        session_actions.addWidget(self.session_reexport_button)
        session_actions.addWidget(self.session_delete_button)
        layout.addLayout(session_actions)
        return page

    def _build_recording_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("工作階段標題"))
        self.recording_title_edit = QLineEdit()
        self.recording_title_edit.setPlaceholderText("留空會自動使用日期與時間")
        title_row.addWidget(self.recording_title_edit, 1)
        layout.addLayout(title_row)

        notice = QLabel(
            "按下開始後再播放直播或影片。錄製會持續到你按下停止；"
            f"安全上限為 {int(GUI_SAFETY_DURATION_SECONDS / 3600)} 小時。"
        )
        notice.setObjectName("mutedText")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        self.recording_configuration_label = QLabel()
        self.recording_configuration_label.setObjectName("secretStatus")
        self.recording_configuration_label.setWordWrap(True)
        layout.addWidget(self.recording_configuration_label)

        status_frame = QFrame()
        status_frame.setObjectName("recordingPanel")
        status_layout = QGridLayout(status_frame)
        self.recording_status_label = QLabel("尚未開始")
        self.recording_status_label.setObjectName("recordingStatus")
        self.recording_status_label.setWordWrap(True)
        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("recordingClock")
        self.audio_level = QProgressBar()
        self.audio_level.setRange(0, 100)
        self.audio_level.setValue(0)
        self.audio_level.setTextVisible(False)
        self.queue_label = QLabel("Chunk 0 · 待處理 0 · 積壓 0.0 秒")
        self.queue_label.setObjectName("mutedText")
        status_layout.addWidget(self.recording_status_label, 0, 0)
        status_layout.addWidget(self.elapsed_label, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
        status_layout.addWidget(QLabel("系統音訊音量"), 1, 0)
        status_layout.addWidget(self.audio_level, 1, 1)
        status_layout.addWidget(self.queue_label, 2, 0, 1, 2)
        layout.addWidget(status_frame)

        preview_title = QLabel("即時逐字稿預覽（尚未進行說話者 A／B 標註）")
        preview_title.setObjectName("sectionTitle")
        layout.addWidget(preview_title)
        self.transcript_preview = QTextEdit()
        self.transcript_preview.setReadOnly(True)
        self.transcript_preview.setPlaceholderText(
            "每個音訊 Chunk 完成辨識後會顯示在這裡，約有數十秒延遲。"
        )
        layout.addWidget(self.transcript_preview, 1)

        actions = QHBoxLayout()
        self.open_output_button = QPushButton("開啟輸出資料夾")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self._open_recording_output)
        self.stop_recording_button = QPushButton("停止並完成處理")
        self.stop_recording_button.setEnabled(False)
        self.stop_recording_button.clicked.connect(self.stop_recording)
        self.start_recording_button = QPushButton("開始錄製")
        self.start_recording_button.setObjectName("primaryButton")
        self.start_recording_button.clicked.connect(self.start_recording)
        actions.addWidget(self.open_output_button)
        actions.addStretch(1)
        actions.addWidget(self.stop_recording_button)
        actions.addWidget(self.start_recording_button)
        layout.addLayout(actions)
        return page

    def _build_ai_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(14)

        notice = QLabel(
            "只會將所選工作階段的逐字稿文字、說話者標籤與時間戳傳送至 "
            "OpenAI，不會傳送原始音訊。執行會使用 API 額度；成功的文字批次會保留快取。"
        )
        notice.setObjectName("mutedText")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("工作階段"))
        self.ai_session_combo = QComboBox()
        self.ai_session_combo.currentIndexChanged.connect(self._load_ai_session)
        selector_row.addWidget(self.ai_session_combo, 1)
        refresh = QPushButton("重新整理")
        refresh.clicked.connect(self.refresh_ai_sessions)
        selector_row.addWidget(refresh)
        layout.addLayout(selector_row)

        self.ai_session_info_label = QLabel(
            f"模型：OpenAI / {DEFAULT_OPENAI_MODEL}"
        )
        self.ai_session_info_label.setObjectName("secretStatus")
        self.ai_session_info_label.setWordWrap(True)
        layout.addWidget(self.ai_session_info_label)

        progress_row = QHBoxLayout()
        self.ai_status_label = QLabel("請選擇具有逐字稿的工作階段。")
        self.ai_status_label.setWordWrap(True)
        self.ai_progress_bar = QProgressBar()
        self.ai_progress_bar.setRange(0, 100)
        self.ai_progress_bar.setValue(0)
        self.ai_progress_bar.setMinimumWidth(150)
        self.ai_progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_row.addWidget(self.ai_status_label, 1)
        progress_row.addWidget(self.ai_progress_bar)
        layout.addLayout(progress_row)

        summary_title = QLabel("章節與完整摘要")
        summary_title.setObjectName("sectionTitle")
        layout.addWidget(summary_title)
        self.ai_summary_preview = QTextEdit()
        self.ai_summary_preview.setReadOnly(True)
        self.ai_summary_preview.setPlaceholderText(
            "完成 AI 分析後，章節標題、時間、摘要與重點會顯示在這裡。"
        )
        layout.addWidget(self.ai_summary_preview, 1)

        actions = QHBoxLayout()
        self.ai_open_output_button = QPushButton("開啟輸出資料夾")
        self.ai_open_output_button.setEnabled(False)
        self.ai_open_output_button.clicked.connect(self._open_ai_output)
        self.ai_open_summary_button = QPushButton("開啟 summary.md")
        self.ai_open_summary_button.setEnabled(False)
        self.ai_open_summary_button.clicked.connect(self._open_ai_summary)
        self.ai_cancel_button = QPushButton("停止分析")
        self.ai_cancel_button.setEnabled(False)
        self.ai_cancel_button.clicked.connect(self.cancel_ai_analysis)
        self.ai_start_button = QPushButton("產生 AI 章節摘要")
        self.ai_start_button.setObjectName("primaryButton")
        self.ai_start_button.setEnabled(False)
        self.ai_start_button.clicked.connect(self.start_ai_analysis)
        actions.addWidget(self.ai_open_output_button)
        actions.addWidget(self.ai_open_summary_button)
        actions.addStretch(1)
        actions.addWidget(self.ai_cancel_button)
        actions.addWidget(self.ai_start_button)
        layout.addLayout(actions)
        return page

    def _build_maintenance_tab(self) -> QWidget:
        page = QWidget()
        outer_layout = QVBoxLayout(page)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(14)

        notice = QLabel(
            "Whisper 模型下載到設定的模型資料夾，不會包進逐字稿匯出檔。"
            "未完成工作階段的 SQLite 與待處理 WAV 會保留到復原成功。"
        )
        notice.setObjectName("mutedText")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        model_title = QLabel("Whisper 模型")
        model_title.setObjectName("sectionTitle")
        layout.addWidget(model_title)
        self.model_root_label = QLabel()
        self.model_root_label.setObjectName("secretStatus")
        self.model_root_label.setWordWrap(True)
        layout.addWidget(self.model_root_label)

        self.model_status_table = QTableWidget(0, 4)
        self.model_status_table.setHorizontalHeaderLabels(
            ["模型", "狀態", "大小", "位置"]
        )
        self.model_status_table.verticalHeader().setVisible(False)
        self.model_status_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.model_status_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.model_status_table.setMinimumHeight(180)
        self.model_status_table.setMaximumHeight(220)
        model_header = self.model_status_table.horizontalHeader()
        model_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        model_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        model_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        model_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.model_status_table)

        model_progress_row = QHBoxLayout()
        self.model_operation_label = QLabel("可按需要下載 small 或 medium 模型。")
        self.model_operation_label.setWordWrap(True)
        self.model_progress_bar = QProgressBar()
        self.model_progress_bar.setRange(0, 100)
        self.model_progress_bar.setValue(0)
        self.model_progress_bar.setMinimumWidth(150)
        self.model_progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        model_progress_row.addWidget(self.model_operation_label, 1)
        model_progress_row.addWidget(self.model_progress_bar)
        layout.addLayout(model_progress_row)

        model_actions = QHBoxLayout()
        self.model_name_combo = QComboBox()
        for name in ModelManager.supported_model_names():
            self.model_name_combo.addItem(name, name)
        self.model_name_combo.currentIndexChanged.connect(
            self._update_model_action_state
        )
        self.model_refresh_button = QPushButton("重新檢查模型")
        self.model_refresh_button.clicked.connect(self.refresh_model_statuses)
        self.model_open_button = QPushButton("開啟模型資料夾")
        self.model_open_button.clicked.connect(self._open_model_directory)
        self.model_delete_button = QPushButton("移除選定模型")
        self.model_delete_button.clicked.connect(self.delete_selected_model)
        self.model_download_button = QPushButton("下載選定模型")
        self.model_download_button.setObjectName("primaryButton")
        self.model_download_button.clicked.connect(self.start_model_download)
        model_actions.addWidget(self.model_name_combo)
        model_actions.addWidget(self.model_refresh_button)
        model_actions.addWidget(self.model_open_button)
        model_actions.addStretch(1)
        model_actions.addWidget(self.model_delete_button)
        model_actions.addWidget(self.model_download_button)
        layout.addLayout(model_actions)

        recovery_title = QLabel("未完成工作階段")
        recovery_title.setObjectName("sectionTitle")
        layout.addWidget(recovery_title)
        recovery_selector = QHBoxLayout()
        recovery_selector.addWidget(QLabel("工作階段"))
        self.recovery_session_combo = QComboBox()
        self.recovery_session_combo.currentIndexChanged.connect(
            self._load_recovery_session
        )
        recovery_selector.addWidget(self.recovery_session_combo, 1)
        self.recovery_refresh_button = QPushButton("重新整理")
        self.recovery_refresh_button.clicked.connect(
            self.refresh_recovery_sessions
        )
        recovery_selector.addWidget(self.recovery_refresh_button)
        layout.addLayout(recovery_selector)

        self.recovery_info_label = QLabel("目前沒有需要復原的工作階段。")
        self.recovery_info_label.setObjectName("secretStatus")
        self.recovery_info_label.setWordWrap(True)
        layout.addWidget(self.recovery_info_label)
        recovery_progress_row = QHBoxLayout()
        self.recovery_status_label = QLabel("復原會沿用原工作階段的模型與說話者設定。")
        self.recovery_status_label.setWordWrap(True)
        self.recovery_progress_bar = QProgressBar()
        self.recovery_progress_bar.setRange(0, 100)
        self.recovery_progress_bar.setValue(0)
        self.recovery_progress_bar.setMinimumWidth(150)
        self.recovery_progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        recovery_progress_row.addWidget(self.recovery_status_label, 1)
        recovery_progress_row.addWidget(self.recovery_progress_bar)
        layout.addLayout(recovery_progress_row)

        recovery_actions = QHBoxLayout()
        self.recovery_open_pending_button = QPushButton("開啟待處理音訊位置")
        self.recovery_open_pending_button.clicked.connect(
            self._open_pending_audio_directory
        )
        self.recovery_open_output_button = QPushButton("開啟復原輸出")
        self.recovery_open_output_button.setEnabled(False)
        self.recovery_open_output_button.clicked.connect(
            self._open_recovery_output
        )
        self.recovery_cancel_button = QPushButton("停止復原")
        self.recovery_cancel_button.setEnabled(False)
        self.recovery_cancel_button.clicked.connect(self.cancel_session_recovery)
        self.recovery_start_button = QPushButton("復原並重新匯出")
        self.recovery_start_button.setObjectName("primaryButton")
        self.recovery_start_button.setEnabled(False)
        self.recovery_start_button.clicked.connect(self.start_session_recovery)
        recovery_actions.addWidget(self.recovery_open_pending_button)
        recovery_actions.addWidget(self.recovery_open_output_button)
        recovery_actions.addStretch(1)
        recovery_actions.addWidget(self.recovery_cancel_button)
        recovery_actions.addWidget(self.recovery_start_button)
        layout.addLayout(recovery_actions)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer_layout = QVBoxLayout(page)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 24, 18, 18)
        layout.setSpacing(18)

        intro = QLabel(
            "一般選項保存在本機設定檔；API Key 與 Hugging Face Token "
            "則保存於目前 Windows 使用者的憑證管理員。環境變數若存在會優先使用。"
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedText")
        layout.addWidget(intro)

        preferences_title = QLabel("錄製與儲存")
        preferences_title.setObjectName("sectionTitle")
        layout.addWidget(preferences_title)
        preferences_card = QFrame()
        preferences_card.setObjectName("sectionCard")
        preferences_layout = QVBoxLayout(preferences_card)
        preferences_layout.setContentsMargins(18, 18, 18, 18)
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(14)

        self.model_directory_edit = QLineEdit()
        self.model_directory_edit.setObjectName("modelDirectoryEdit")
        form.addRow("模型資料夾", self._directory_field(self.model_directory_edit))

        self.export_directory_edit = QLineEdit()
        self.export_directory_edit.setObjectName("exportDirectoryEdit")
        form.addRow("預設匯出資料夾", self._directory_field(self.export_directory_edit))

        self.whisper_model_combo = QComboBox()
        for name in ModelManager.supported_model_names():
            self.whisper_model_combo.addItem(name, name)
        form.addRow("Whisper 模型", self.whisper_model_combo)

        self.compute_device_combo = QComboBox()
        for label, value in (
            ("自動（NVIDIA CUDA 優先，失敗時 CPU）", "auto"),
            ("CPU", "cpu"),
            ("NVIDIA CUDA", "cuda"),
            ("AMD 實驗模式（目前回退 CPU）", "amd_experimental"),
        ):
            self.compute_device_combo.addItem(label, value)
        form.addRow("Whisper 運算", self.compute_device_combo)

        speaker_row = QWidget()
        speaker_layout = QHBoxLayout(speaker_row)
        speaker_layout.setContentsMargins(0, 0, 0, 0)
        self.speaker_mode_combo = QComboBox()
        self.speaker_mode_combo.addItem(
            "不區分說話者（不執行 A／B 分析）",
            "off",
        )
        self.speaker_mode_combo.addItem("自動判斷 2～6 人（仍執行分析）", "auto")
        self.speaker_mode_combo.addItem("指定人數", "fixed")
        self.speaker_mode_combo.currentIndexChanged.connect(
            self._update_speaker_count_enabled
        )
        self.speaker_count_combo = QComboBox()
        for count in range(2, 7):
            self.speaker_count_combo.addItem(f"{count} 人", count)
        speaker_layout.addWidget(self.speaker_mode_combo, 1)
        speaker_layout.addWidget(self.speaker_count_combo)
        form.addRow("說話者分析", speaker_row)

        self.diarization_device_combo = QComboBox()
        self.diarization_device_combo.addItem("自動（NVIDIA CUDA 優先）", "auto")
        self.diarization_device_combo.addItem("CPU", "cpu")
        self.diarization_device_combo.addItem("NVIDIA CUDA", "cuda")
        form.addRow("說話者分析運算", self.diarization_device_combo)

        self.keep_display_checkbox = QCheckBox(
            "錄製期間保持螢幕開啟（程式一律會要求 Windows 不進入睡眠）"
        )
        form.addRow("電源管理", self.keep_display_checkbox)
        preferences_layout.addLayout(form)
        layout.addWidget(preferences_card)

        secrets_title = QLabel("API 與模型授權")
        secrets_title.setObjectName("sectionTitle")
        layout.addWidget(secrets_title)

        secrets_card = QFrame()
        secrets_card.setObjectName("sectionCard")
        secrets_layout = QVBoxLayout(secrets_card)
        secrets_layout.setContentsMargins(18, 18, 18, 18)
        secret_form = QFormLayout()
        openai_field, self.openai_key_edit = self._secret_field(
            "貼上 OpenAI API Key",
            OPENAI_SECRET,
        )
        secret_form.addRow("OpenAI API Key", openai_field)
        huggingface_field, self.huggingface_token_edit = self._secret_field(
            "貼上 Hugging Face Token",
            HUGGINGFACE_SECRET,
        )
        secret_form.addRow("Hugging Face Token", huggingface_field)
        secrets_layout.addLayout(secret_form)

        self.secret_status_label = QLabel()
        self.secret_status_label.setObjectName("secretStatus")
        self.secret_status_label.setWordWrap(True)
        secrets_layout.addWidget(self.secret_status_label)
        layout.addWidget(secrets_card)

        buttons = QHBoxLayout()
        reset_button = QPushButton("重新載入已儲存設定")
        reset_button.clicked.connect(self._load_settings_into_form)
        save_button = QPushButton("儲存設定")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self.save_settings)
        buttons.addStretch(1)
        buttons.addWidget(reset_button)
        buttons.addWidget(save_button)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        buttons.setContentsMargins(18, 4, 18, 14)
        outer_layout.addLayout(buttons)
        return page

    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 24, 18, 18)
        self.diagnostics_text = QTextEdit()
        self.diagnostics_text.setObjectName("diagnosticsText")
        self.diagnostics_text.setReadOnly(True)
        self.diagnostics_text.setPlainText("尚未執行硬體診斷。")
        layout.addWidget(self.diagnostics_text)
        actions = QHBoxLayout()
        refresh = QPushButton("重新檢測")
        refresh.clicked.connect(self.refresh_diagnostics)
        open_data = QPushButton("開啟 UserData")
        open_data.clicked.connect(self._open_data_directory)
        open_logs = QPushButton("開啟日誌資料夾")
        open_logs.clicked.connect(self._open_log_directory)
        export = QPushButton("匯出診斷 JSON")
        export.clicked.connect(self._export_diagnostics_json)
        export_bundle = QPushButton("匯出診斷包 ZIP")
        export_bundle.clicked.connect(self._export_diagnostics_bundle)
        actions.addWidget(refresh)
        actions.addWidget(open_data)
        actions.addWidget(open_logs)
        actions.addStretch(1)
        actions.addWidget(export)
        actions.addWidget(export_bundle)
        layout.addLayout(actions)
        return page

    def _open_data_directory(self) -> None:
        self.paths.ensure()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.root)))

    def _open_log_directory(self) -> None:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.logs)))

    def _export_diagnostics_json(self) -> None:
        if self._last_diagnostics_report is None:
            QMessageBox.information(
                self,
                "尚無診斷資料",
                "請先按下「重新檢測」，完成後再匯出。",
            )
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = self.paths.diagnostics / f"diagnostics_{timestamp}.json"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "匯出診斷 JSON",
            str(default_path),
            "JSON (*.json)",
        )
        if not selected:
            return
        destination = Path(selected).expanduser().resolve()
        if destination.suffix.lower() != ".json":
            destination = destination.with_suffix(".json")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(
                    self._last_diagnostics_report,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self.logger.error("Diagnostic export failed: type=%s", type(exc).__name__)
            QMessageBox.warning(self, "匯出失敗", str(exc))
            return
        self.logger.info("Diagnostic JSON exported")
        self.statusBar().showMessage(f"診斷已匯出：{destination}", 7000)

    def _export_diagnostics_bundle(self) -> None:
        if self._last_diagnostics_report is None:
            QMessageBox.information(
                self,
                "尚無診斷資料",
                "請先按下「重新檢測」，完成後再匯出。",
            )
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = self.paths.diagnostics / f"diagnostics_bundle_{timestamp}.zip"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "匯出診斷包 ZIP",
            str(default_path),
            "ZIP (*.zip)",
        )
        if not selected:
            return
        destination = Path(selected).expanduser().resolve()
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        manifest = {
            "application": "VideoChapterTool",
            "version": __version__,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "privacy": (
                "不包含逐字稿、音訊、SQLite、API Key、Hugging Face Token 或原始設定檔"
            ),
        }
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                destination,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                )
                archive.writestr(
                    "diagnostics.json",
                    json.dumps(
                        self._last_diagnostics_report,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
                for log_path in sorted(self.paths.logs.glob("application.log*")):
                    if log_path.is_file():
                        archive.write(log_path, f"Logs/{log_path.name}")
        except (OSError, zipfile.BadZipFile) as exc:
            self.logger.error(
                "Diagnostic bundle export failed: type=%s",
                type(exc).__name__,
            )
            QMessageBox.warning(self, "匯出失敗", str(exc))
            return
        self.logger.info("Diagnostic support bundle exported")
        self.statusBar().showMessage(f"診斷包已匯出：{destination}", 7000)

    def _directory_field(self, line_edit: QLineEdit) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("選擇…")
        browse.clicked.connect(lambda: self._choose_directory(line_edit))
        layout.addWidget(line_edit, 1)
        layout.addWidget(browse)
        return container

    def _secret_field(
        self,
        placeholder: str,
        secret_name: str,
    ) -> tuple[QWidget, QLineEdit]:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(container)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(placeholder)
        save = QPushButton("保存", container)
        save.clicked.connect(lambda: self._save_secret(secret_name, edit))
        delete = QPushButton("刪除", container)
        delete.clicked.connect(lambda: self._delete_secret(secret_name))
        layout.addWidget(edit, 1)
        layout.addWidget(save)
        layout.addWidget(delete)
        return container, edit

    def _choose_directory(self, line_edit: QLineEdit) -> None:
        current = line_edit.text().strip() or str(self.paths.root)
        selected = QFileDialog.getExistingDirectory(self, "選擇資料夾", current)
        if selected:
            line_edit.setText(str(Path(selected).resolve()))

    def _set_combo_data(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(0, index))

    def _load_settings_into_form(self) -> None:
        self.settings = self.settings_store.load_or_create()
        self.model_directory_edit.setText(self.settings.model_directory)
        self.export_directory_edit.setText(self.settings.export_directory)
        self._set_combo_data(
            self.whisper_model_combo,
            self.settings.whisper_model,
        )
        self._set_combo_data(
            self.model_name_combo,
            self.settings.whisper_model,
        )
        self._set_combo_data(self.compute_device_combo, self.settings.compute_device)
        self._set_combo_data(self.speaker_mode_combo, self.settings.speaker_mode)
        self._set_combo_data(
            self.speaker_count_combo,
            self.settings.speaker_count,
        )
        self._set_combo_data(
            self.diarization_device_combo,
            self.settings.diarization_device,
        )
        self.keep_display_checkbox.setChecked(
            self.settings.keep_display_on_during_recording
        )
        self._update_speaker_count_enabled()
        self._update_recording_configuration_label(self.settings)

    @Slot()
    def _update_speaker_count_enabled(self) -> None:
        fixed = self.speaker_mode_combo.currentData() == "fixed"
        self.speaker_count_combo.setEnabled(fixed)
        self.diarization_device_combo.setEnabled(
            self.speaker_mode_combo.currentData() != "off"
        )

    @Slot()
    def save_settings(self) -> None:
        self._persist_form_settings(show_success=True)

    def _persist_form_settings(self, *, show_success: bool) -> bool:
        try:
            settings = AppSettings(
                schema_version=self.settings.schema_version,
                language=self.settings.language,
                whisper_model=str(self.whisper_model_combo.currentData()),
                compute_device=str(self.compute_device_combo.currentData()),
                model_directory=self.model_directory_edit.text().strip(),
                export_directory=self.export_directory_edit.text().strip(),
                log_level=self.settings.log_level,
                traditional_chinese_output=self.settings.traditional_chinese_output,
                speaker_mode=str(self.speaker_mode_combo.currentData()),
                speaker_count=int(self.speaker_count_combo.currentData()),
                diarization_device=str(self.diarization_device_combo.currentData()),
                keep_display_on_during_recording=(
                    self.keep_display_checkbox.isChecked()
                ),
            )
            settings.validate()
            Path(settings.model_directory).expanduser().mkdir(parents=True, exist_ok=True)
            Path(settings.export_directory).expanduser().mkdir(parents=True, exist_ok=True)
            self.settings_store.save(settings)
        except (OSError, SettingsError, ValueError) as exc:
            self.logger.error("Settings save failed: type=%s", type(exc).__name__)
            QMessageBox.critical(self, "無法儲存設定", str(exc))
            return False
        self.settings = settings
        self._set_combo_data(self.model_name_combo, settings.whisper_model)
        if show_success and not self._maintenance_busy():
            self.refresh_model_statuses()
        self._update_recording_configuration_label(settings)
        if show_success:
            self.logger.info(
                "Settings saved: model=%s compute=%s speakers=%s",
                settings.whisper_model,
                settings.compute_device,
                settings.speaker_mode,
            )
            self.statusBar().showMessage("設定已儲存", 5000)
        return True

    def _update_recording_configuration_label(self, settings: AppSettings) -> None:
        if settings.speaker_mode == "off":
            speaker_text = "不區分說話者（不執行 A／B 分析）"
        elif settings.speaker_mode == "fixed":
            speaker_text = f"指定 {settings.speaker_count} 人"
        else:
            speaker_text = "自動判斷 2～6 人"
        display_text = (
            "保持螢幕開啟"
            if settings.keep_display_on_during_recording
            else "允許螢幕依 Windows 設定關閉"
        )
        self.recording_configuration_label.setText(
            f"本次設定：說話者 {speaker_text}　·　"
            f"Whisper {settings.whisper_model}/{settings.compute_device}　·　"
            f"{display_text}"
        )

    def _secret_state_text(self, secret_name: str) -> tuple[bool, str]:
        try:
            state = self.secret_store.state(secret_name)
        except CredentialError:
            return False, "無法讀取"
        if not state.configured:
            return False, "未設定"
        if state.source == "environment":
            return True, "已由環境變數設定"
        return True, "已安全保存於 Windows 憑證管理員"

    def _refresh_secret_status(self) -> None:
        openai_ready, openai_text = self._secret_state_text(OPENAI_SECRET)
        hf_ready, hf_text = self._secret_state_text(HUGGINGFACE_SECRET)
        self.secret_status_label.setText(
            f"OpenAI API Key：{openai_text}　｜　"
            f"Hugging Face Token：{hf_text}"
        )
        self.cloud_card.set_content(
            "可使用" if openai_ready else "本機功能可使用",
            f"OpenAI：{openai_text} · Hugging Face：{hf_text}",
        )

    def _save_secret(self, secret_name: str, edit: QLineEdit) -> None:
        value = edit.text().strip()
        if not value:
            QMessageBox.warning(self, "沒有可保存的內容", "請先貼上金鑰或 Token。")
            return
        try:
            self.secret_store.set(secret_name, value)
        except CredentialError as exc:
            QMessageBox.critical(self, "無法保存金鑰", str(exc))
            return
        edit.clear()
        self._refresh_secret_status()
        self.statusBar().showMessage("金鑰已保存於 Windows 憑證管理員", 5000)

    def _delete_secret(self, secret_name: str, *, confirm: bool = True) -> None:
        if confirm:
            answer = QMessageBox.question(
                self,
                "刪除已保存的金鑰",
                "確定要從 Windows 憑證管理員刪除嗎？環境變數不會受影響。",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            deleted = self.secret_store.delete(secret_name)
        except CredentialError as exc:
            QMessageBox.critical(self, "無法刪除金鑰", str(exc))
            return
        self._refresh_secret_status()
        self.statusBar().showMessage(
            "已刪除保存的金鑰" if deleted else "沒有找到已保存的金鑰",
            5000,
        )

    def _maintenance_busy(self) -> bool:
        return self._maintenance_worker is not None

    @Slot()
    def refresh_model_statuses(self) -> None:
        try:
            manager = ModelManager(self.settings.model_directory)
            statuses = manager.list_statuses()
        except (ModelManagementError, OSError, ValueError) as exc:
            self.model_operation_label.setText(f"模型狀態讀取失敗：{exc}")
            return
        self._model_statuses = {status.name: status for status in statuses}
        self._refresh_whisper_model_choices()
        self.model_root_label.setText(f"模型資料夾：{manager.root}")
        self.model_status_table.setRowCount(len(statuses))
        for row, status in enumerate(statuses):
            if status.valid:
                state = "可使用"
            elif status.installed:
                state = "內容不完整"
            elif status.partial_download_exists:
                state = "下載未完成，可續傳"
            else:
                state = "尚未下載"
            values = (
                status.name,
                state,
                _file_size_text(status.size_bytes),
                status.path,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 1, 2}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.model_status_table.setItem(row, column, item)
        self._update_model_action_state()

    def _refresh_whisper_model_choices(self) -> None:
        model = self.whisper_model_combo.model()
        for index in range(self.whisper_model_combo.count()):
            name = str(self.whisper_model_combo.itemData(index) or "")
            status = self._model_statuses.get(name)
            if status and status.valid:
                state = "可使用"
                enabled = True
            elif status and status.installed:
                state = "內容不完整"
                enabled = False
            elif status and status.partial_download_exists:
                state = "下載未完成"
                enabled = False
            else:
                state = "尚未下載"
                enabled = False
            self.whisper_model_combo.setItemText(index, f"{name}（{state}）")
            if isinstance(model, QStandardItemModel):
                item = model.item(index)
                if item is not None:
                    item.setEnabled(enabled)

    @Slot()
    def _update_model_action_state(self) -> None:
        name = str(self.model_name_combo.currentData() or "")
        status = self._model_statuses.get(name)
        busy = (
            self._maintenance_busy()
            or self._recording_workflow is not None
            or self._ai_workflow is not None
        )
        valid = bool(status and status.valid)
        installed = bool(status and status.installed)
        self.model_download_button.setEnabled(not busy and not valid and not installed)
        self.model_delete_button.setEnabled(not busy and installed)
        if valid:
            self.model_download_button.setText("模型已可使用")
            if not self._maintenance_busy():
                self.model_progress_bar.setRange(0, 100)
                self.model_progress_bar.setValue(100)
                self.model_operation_label.setText(
                    f"{name} 已完成驗證，可供錄製與復原使用。"
                )
        elif installed:
            self.model_download_button.setText("請先移除不完整模型")
            if not self._maintenance_busy():
                self.model_progress_bar.setRange(0, 100)
                self.model_progress_bar.setValue(0)
                self.model_operation_label.setText(
                    f"{name} 模型內容不完整；確認後移除，再重新下載。"
                )
        elif status and status.partial_download_exists:
            self.model_download_button.setText("繼續下載")
            if not self._maintenance_busy():
                self.model_progress_bar.setRange(0, 100)
                self.model_progress_bar.setValue(0)
                self.model_operation_label.setText(
                    f"{name} 有未完成下載，可從現有資料繼續。"
                )
        else:
            self.model_download_button.setText("下載選定模型")
            if not self._maintenance_busy():
                self.model_progress_bar.setRange(0, 100)
                self.model_progress_bar.setValue(0)
                self.model_operation_label.setText(f"{name} 尚未下載。")

    @Slot()
    def _open_model_directory(self) -> None:
        path = Path(self.settings.model_directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    @Slot()
    def start_model_download(self) -> None:
        if self._maintenance_busy():
            return
        if self._recording_workflow is not None or self._ai_workflow is not None:
            QMessageBox.warning(
                self,
                "背景工作正在進行",
                "請先完成錄製或 AI 分析再下載模型。",
            )
            return
        name = str(self.model_name_combo.currentData() or "")
        status = self._model_statuses.get(name)
        if status is None:
            return
        if status.valid:
            QMessageBox.information(self, "模型已可使用", f"{name} 已完成驗證。")
            return
        question = (
            f"要將 {name} 模型下載到以下位置嗎？\n\n{status.path}\n\n"
            "下載可能需要數分鐘；未完成資料會保留供下次續傳。"
        )
        if (
            QMessageBox.question(self, "確認下載 Whisper 模型", question)
            != QMessageBox.StandardButton.Yes
        ):
            return
        manager = ModelManager(self.settings.model_directory)
        worker = FunctionWorker(lambda: manager.download(name))
        worker.signals.result.connect(self._model_download_completed)
        worker.signals.error.connect(self._model_download_failed)
        worker.signals.finished.connect(self._maintenance_finished)
        self._maintenance_worker = worker
        self._maintenance_task = "model"
        self.logger.info("Whisper model download started: model=%s", name)
        self._workers.add(worker)
        self.model_progress_bar.setRange(0, 0)
        self.model_operation_label.setText(
            f"正在下載並驗證 {name}；請保持網路連線。"
        )
        self._set_maintenance_controls_running(True)
        self.thread_pool.start(worker)

    @Slot(object)
    def _model_download_completed(self, result: object) -> None:
        if not isinstance(result, ModelStatus):
            self._model_download_failed("模型下載完成，但收到無法辨識的結果。")
            return
        self.model_progress_bar.setRange(0, 100)
        self.model_progress_bar.setValue(100)
        self.model_operation_label.setText(
            f"{result.name} 模型準備完成，大小 {_file_size_text(result.size_bytes)}。"
        )
        self.logger.info(
            "Whisper model download completed: model=%s size_bytes=%s",
            result.name,
            result.size_bytes,
        )
        self.statusBar().showMessage(f"{result.name} 模型準備完成", 8000)

    @Slot(str)
    def _model_download_failed(self, message: str) -> None:
        self.logger.error("Whisper model download failed")
        self.model_progress_bar.setRange(0, 100)
        self.model_progress_bar.setValue(0)
        self.model_operation_label.setText("模型下載未完成：" + message)
        self.statusBar().showMessage("模型下載未完成；續傳資料已保留")

    @Slot()
    def delete_selected_model(self) -> None:
        if (
            self._maintenance_busy()
            or self._recording_workflow is not None
            or self._ai_workflow is not None
        ):
            return
        name = str(self.model_name_combo.currentData() or "")
        status = self._model_statuses.get(name)
        if status is None or not status.installed:
            return
        confirmation, accepted = QInputDialog.getText(
            self,
            "移除本機 Whisper 模型",
            f"這只會移除模型資料夾，不會刪除逐字稿。\n"
            f"若確定要移除，請輸入：{name}",
        )
        if not accepted:
            return
        try:
            deleted = ModelManager(self.settings.model_directory).delete(
                name,
                confirmation=confirmation.strip(),
            )
        except ModelManagementError as exc:
            QMessageBox.warning(self, "未移除模型", str(exc))
            return
        self.model_operation_label.setText(
            f"{name} 模型已移除；逐字稿、摘要及設定均未刪除。"
            if deleted
            else f"{name} 模型原本就未安裝。"
        )
        self.refresh_model_statuses()

    @Slot()
    def refresh_recovery_sessions(self) -> None:
        selected = self.recovery_session_combo.currentData()
        try:
            sessions = self.session_store.list_sessions(incomplete_only=True)
        except DatabaseError as exc:
            self.recovery_info_label.setText(f"未完成工作階段讀取失敗：{exc}")
            return
        self.recovery_session_combo.blockSignals(True)
        self.recovery_session_combo.clear()
        for session in sessions:
            pending_count = len(self.session_store.list_pending_chunks(session.id))
            self.recovery_session_combo.addItem(
                f"#{session.id}　{session.title}　"
                f"[{_STATUS_LABELS.get(session.status, session.status)}]　"
                f"待處理 {pending_count}",
                session.id,
            )
        if selected is not None:
            index = self.recovery_session_combo.findData(selected)
            if index >= 0:
                self.recovery_session_combo.setCurrentIndex(index)
        self.recovery_session_combo.blockSignals(False)
        self._load_recovery_session(self.recovery_session_combo.currentIndex())

    @Slot(int)
    def _load_recovery_session(self, _index: int = -1) -> None:
        session_id = self.recovery_session_combo.currentData()
        self._recovery_output_directory = None
        self.recovery_open_output_button.setEnabled(False)
        self.recovery_progress_bar.setValue(0)
        if session_id is None:
            self.recovery_info_label.setText("目前沒有需要復原的工作階段。")
            self.recovery_start_button.setEnabled(False)
            self.recovery_open_pending_button.setEnabled(False)
            return
        try:
            session = self.session_store.get_session(int(session_id))
            pending = self.session_store.list_pending_chunks(int(session_id))
            session_chunks = self.session_store.list_session_chunks(int(session_id))
            segment_count = len(
                self.session_store.get_transcript_segments(int(session_id))
            )
        except DatabaseError as exc:
            self.recovery_info_label.setText(f"工作階段讀取失敗：{exc}")
            self.recovery_start_button.setEnabled(False)
            return
        available_audio = sum(Path(item.path).is_file() for item in pending)
        retained_audio = sum(Path(item.path).is_file() for item in session_chunks)
        detail = (
            f"工作階段 #{session.id}　·　狀態 "
            f"{_STATUS_LABELS.get(session.status, session.status)}　·　"
            f"已保存逐字稿 {segment_count} 段　·　"
            f"待處理 Chunk {len(pending)} 個（可讀 {available_audio} 個）　·　"
            f"保留音訊 {retained_audio} 個"
        )
        if session.error_message:
            detail += f"\n上次錯誤：{session.error_message}"
        if not pending and not segment_count:
            detail += "\n此工作階段沒有已保存內容；復原只會完成空白匯出。"
        self.recovery_info_label.setText(detail)
        busy = (
            self._maintenance_busy()
            or self._recording_workflow is not None
            or self._ai_workflow is not None
        )
        self.recovery_start_button.setEnabled(not busy)
        self.recovery_open_pending_button.setEnabled(retained_audio > 0)

    @Slot()
    def start_session_recovery(self) -> None:
        if self._maintenance_busy():
            return
        if self._recording_workflow is not None or self._ai_workflow is not None:
            QMessageBox.warning(
                self,
                "背景工作正在進行",
                "請先完成錄製或 AI 分析再復原工作階段。",
            )
            return
        if not self._persist_form_settings(show_success=False):
            return
        session_id = self.recovery_session_combo.currentData()
        if session_id is None:
            return
        try:
            session = self.session_store.get_session(int(session_id))
            pending_count = len(
                self.session_store.list_pending_chunks(int(session_id))
            )
            segment_count = len(
                self.session_store.get_transcript_segments(int(session_id))
            )
        except DatabaseError as exc:
            QMessageBox.critical(self, "無法開始復原", str(exc))
            return
        question = (
            f"要復原工作階段 #{session.id} 嗎？\n\n"
            f"已保存逐字稿：{segment_count} 段\n"
            f"待處理音訊：{pending_count} 個 Chunk\n\n"
            "成功後會重新匯出 TXT、JSON、SRT；原始 SQLite 內容不會先被刪除。"
        )
        if not pending_count and not segment_count:
            question += "\n\n此工作階段沒有內容，結果會是空白逐字稿。"
        if (
            QMessageBox.question(self, "確認復原工作階段", question)
            != QMessageBox.StandardButton.Yes
        ):
            return
        signals = MaintenanceSignals()
        signals.status.connect(self._recovery_status_changed)
        signals.progress.connect(self._recovery_progress_changed)
        workflow = SessionRecoveryWorkflow(
            self.paths,
            self.settings,
            SessionRecoveryRequest(int(session_id)),
            callbacks=SessionRecoveryCallbacks(
                on_status=signals.status.emit,
                on_progress=signals.progress.emit,
            ),
            secret_store=self.secret_store,
        )
        worker = FunctionWorker(workflow.run)
        worker.signals.result.connect(self._session_recovery_completed)
        worker.signals.error.connect(self._session_recovery_failed)
        worker.signals.finished.connect(self._maintenance_finished)
        self._maintenance_worker = worker
        self._maintenance_signals = signals
        self._maintenance_task = "recovery"
        self._recovery_workflow = workflow
        self.logger.info("Session recovery started: session_id=%s", session.id)
        self._workers.add(worker)
        self.recovery_progress_bar.setValue(1)
        self.recovery_status_label.setText("正在準備工作階段復原…")
        self._set_maintenance_controls_running(True)
        self.recovery_cancel_button.setEnabled(True)
        self.thread_pool.start(worker)

    @Slot()
    def cancel_session_recovery(self) -> None:
        if self._recovery_workflow is None:
            return
        self._recovery_workflow.request_cancel()
        self.logger.info("Session recovery cancellation requested")
        self.recovery_cancel_button.setEnabled(False)
        self.recovery_status_label.setText(
            "已要求停止；目前 Chunk 完成後停止，已保存資料不會刪除。"
        )

    @Slot(str)
    def _recovery_status_changed(self, message: str) -> None:
        self.recovery_status_label.setText(message)
        self.statusBar().showMessage(message)

    @Slot(object)
    def _recovery_progress_changed(self, progress: object) -> None:
        if not isinstance(progress, RecoveryProgress):
            return
        total = max(1, progress.chunks_total)
        percent = round(80 * progress.chunks_processed / total)
        self.recovery_progress_bar.setValue(max(1, min(80, percent)))
        self.recovery_status_label.setText(
            f"正在復原 Chunk {progress.chunks_processed}/{progress.chunks_total}　·　"
            f"新增逐字稿 {progress.segments_added} 段"
        )

    @Slot(object)
    def _session_recovery_completed(self, outcome: object) -> None:
        if not isinstance(outcome, SessionRecoveryOutcome):
            self._session_recovery_failed("收到無法辨識的復原結果。")
            return
        if outcome.cancelled:
            self.logger.info("Session recovery cancelled")
            self.recovery_status_label.setText(
                "工作階段復原已停止；剩餘音訊與 SQLite 資料已保留。"
            )
            return
        if outcome.export is None:
            self._session_recovery_failed("復原沒有產生匯出結果。")
            return
        self._recovery_output_directory = outcome.export.directory
        self.recovery_open_output_button.setEnabled(True)
        self.recovery_progress_bar.setValue(100)
        recovery = outcome.recovery
        chunk_count = recovery.chunks_processed if recovery else 0
        segment_count = recovery.segments_added if recovery else 0
        message = (
            f"復原完成：處理 {chunk_count} 個 Chunk，新增 {segment_count} 段逐字稿"
        )
        if outcome.speaker_turns:
            message += f"，說話者區段 {outcome.speaker_turns} 個"
        if outcome.warning:
            message += f"。注意：{outcome.warning}"
        self.recovery_status_label.setText(message)
        self.logger.info(
            "Session recovery completed: chunks=%s segments=%s",
            chunk_count,
            segment_count,
        )
        self.statusBar().showMessage("工作階段復原完成", 8000)

    @Slot(str)
    def _session_recovery_failed(self, message: str) -> None:
        self.logger.error("Session recovery failed")
        self.recovery_progress_bar.setValue(0)
        self.recovery_status_label.setText("工作階段復原未完成：" + message)
        self.statusBar().showMessage("復原未完成；SQLite 與待處理音訊已保留")

    def _set_maintenance_controls_running(self, running: bool) -> None:
        enabled = not running
        self.model_name_combo.setEnabled(enabled)
        self.model_refresh_button.setEnabled(enabled)
        self.model_delete_button.setEnabled(enabled)
        self.model_download_button.setEnabled(enabled)
        self.recovery_session_combo.setEnabled(enabled)
        self.recovery_refresh_button.setEnabled(enabled)
        self.recovery_start_button.setEnabled(enabled)
        self.start_recording_button.setEnabled(enabled)
        self.start_button.setEnabled(enabled)
        self.ai_start_button.setEnabled(enabled and self.ai_session_combo.currentData() is not None)
        self._update_session_action_state()

    @Slot()
    def _maintenance_finished(self) -> None:
        recovered_output = self._recovery_output_directory
        worker = self._maintenance_worker
        if worker is not None:
            self._workers.discard(worker)
        self._maintenance_worker = None
        self._maintenance_signals = None
        self._maintenance_task = None
        self._recovery_workflow = None
        self.recovery_cancel_button.setEnabled(False)
        self._set_maintenance_controls_running(False)
        self.refresh_model_statuses()
        self.refresh_recovery_sessions()
        self.refresh_sessions()
        self.refresh_ai_sessions()
        if recovered_output is not None and recovered_output.is_dir():
            self._recovery_output_directory = recovered_output
            self.recovery_open_output_button.setEnabled(True)
            self.recovery_progress_bar.setValue(100)
        if self._close_after_maintenance:
            self._close_after_maintenance = False
            QTimer.singleShot(0, self.close)

    @Slot()
    def _open_pending_audio_directory(self) -> None:
        session_id = self.recovery_session_combo.currentData()
        if session_id is None:
            return
        try:
            pending = self.session_store.list_pending_chunks(int(session_id))
            session_chunks = self.session_store.list_session_chunks(int(session_id))
        except DatabaseError:
            return
        available = [
            item for item in (*pending, *session_chunks) if Path(item.path).is_file()
        ]
        if available:
            directory = Path(available[0].path).expanduser().resolve().parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    @Slot()
    def _open_recovery_output(self) -> None:
        if self._recovery_output_directory is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._recovery_output_directory))
            )

    @Slot()
    def _show_recording_tab(self) -> None:
        self.tabs.setCurrentIndex(self.recording_tab_index)
        self.recording_title_edit.setFocus()

    @Slot()
    def start_recording(self) -> None:
        if self._recording_workflow is not None:
            return
        if self._maintenance_busy():
            QMessageBox.warning(
                self,
                "模型或復原工作正在進行",
                "請先完成目前的背景工作再開始錄製。",
            )
            return
        if self._ai_workflow is not None:
            QMessageBox.warning(self, "AI 分析正在進行", "請先完成 AI 分析再開始錄製。")
            return
        if not self._persist_form_settings(show_success=False):
            return
        try:
            model_status = ModelManager(self.settings.model_directory).status(
                self.settings.whisper_model
            )
        except (OSError, SettingsError, ValueError) as exc:
            QMessageBox.critical(self, "無法開始錄製", str(exc))
            return
        if not model_status.valid:
            QMessageBox.warning(
                self,
                "Whisper 模型尚未準備完成",
                f"請先下載並驗證 {self.settings.whisper_model} 模型。\n"
                f"模型路徑：{model_status.path}",
            )
            return

        self.transcript_preview.clear()
        self.audio_level.setValue(0)
        self.elapsed_label.setText("00:00")
        self.queue_label.setText("Chunk 0 · 待處理 0 · 積壓 0.0 秒")
        self.recording_status_label.setText("正在準備錄製…")
        self._recording_output_directory = None
        self.open_output_button.setEnabled(False)
        self.start_recording_button.setEnabled(False)
        self.stop_recording_button.setEnabled(True)
        self.recording_title_edit.setEnabled(False)
        self.start_button.setEnabled(False)
        self.refresh_button.setEnabled(False)

        signals = RecordingSignals()
        signals.status.connect(self._recording_status_changed)
        signals.capture_progress.connect(self._capture_progress_changed)
        signals.stream_progress.connect(self._stream_progress_changed)
        signals.segments.connect(self._append_preview_segments)
        signals.session_created.connect(self._recording_session_created)
        callbacks = RecordingCallbacks(
            on_status=signals.status.emit,
            on_capture_progress=signals.capture_progress.emit,
            on_stream_progress=signals.stream_progress.emit,
            on_segments=signals.segments.emit,
            on_session_created=signals.session_created.emit,
        )
        workflow = RecordingWorkflow(
            self.paths,
            self.settings,
            RecordingRequest(
                title=self.recording_title_edit.text(),
                keep_display_on=self.settings.keep_display_on_during_recording,
            ),
            callbacks=callbacks,
            secret_store=self.secret_store,
        )
        worker = FunctionWorker(workflow.run)
        worker.signals.result.connect(self._recording_completed)
        worker.signals.error.connect(self._recording_failed)
        worker.signals.finished.connect(self._recording_finished)
        self._recording_signals = signals
        self._recording_workflow = workflow
        self._recording_worker = worker
        self._workers.add(worker)
        self._update_session_action_state()
        self.logger.info(
            "Recording started: model=%s compute=%s speaker_mode=%s",
            self.settings.whisper_model,
            self.settings.compute_device,
            self.settings.speaker_mode,
        )
        self._update_model_action_state()
        self._load_recovery_session(self.recovery_session_combo.currentIndex())
        self.thread_pool.start(worker)

    @Slot()
    def stop_recording(self) -> None:
        if self._recording_workflow is None:
            return
        self._recording_workflow.request_stop()
        self.logger.info("Recording stop requested")
        self.stop_recording_button.setEnabled(False)
        self.recording_status_label.setText("已要求停止，正在完成最後一段音訊…")

    @Slot(str)
    def _recording_status_changed(self, message: str) -> None:
        self.recording_status_label.setText(message)
        self.statusBar().showMessage(message)

    @Slot(object)
    def _capture_progress_changed(self, progress: object) -> None:
        elapsed = float(getattr(progress, "elapsed_seconds", 0.0))
        peak = float(getattr(progress, "peak_level", 0.0))
        self.elapsed_label.setText(format_duration(elapsed))
        self.audio_level.setValue(max(0, min(100, round(peak * 100))))

    @Slot(object)
    def _stream_progress_changed(self, progress: object) -> None:
        produced = int(getattr(progress, "chunks_produced", 0))
        queued = int(getattr(progress, "queued_chunks", 0))
        backlog = float(getattr(progress, "estimated_backlog_seconds", 0.0))
        self.queue_label.setText(
            f"Chunk {produced} · 待處理 {queued} · 積壓 {backlog:.1f} 秒"
        )

    @Slot(object)
    def _append_preview_segments(self, segments: object) -> None:
        for segment in tuple(segments):
            text = str(getattr(segment, "text", "")).strip()
            if text:
                start = float(getattr(segment, "start", 0.0))
                self.transcript_preview.append(f"[{format_duration(start)}] {text}")
        scroll_bar = self.transcript_preview.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    @Slot(int)
    def _recording_session_created(self, session_id: int) -> None:
        self.logger.info("Recording session created: session_id=%s", session_id)
        self.statusBar().showMessage(f"工作階段 #{session_id} 已建立")
        self.refresh_sessions()

    @Slot(object)
    def _recording_completed(self, outcome: object) -> None:
        result = outcome
        if not isinstance(result, RecordingOutcome):
            self._recording_failed("錄製完成，但收到無法辨識的結果。")
            return
        self._recording_output_directory = result.export.directory
        self.open_output_button.setEnabled(True)
        speaker_text = (
            f"，說話者區段 {result.speaker_turns} 個"
            if result.speaker_turns
            else ""
        )
        self.recording_status_label.setText(
            f"完成：工作階段 #{result.session_id}，"
            f"{format_duration(result.captured_duration_seconds)}，"
            f"逐字稿片段 {result.segments} 個{speaker_text}。"
        )
        if result.warning:
            self.recording_status_label.setText(
                self.recording_status_label.text() + "\n" + result.warning
            )
        self.logger.info(
            "Recording completed: session_id=%s duration_seconds=%.3f segments=%s speaker_turns=%s",
            result.session_id,
            result.captured_duration_seconds,
            result.segments,
            result.speaker_turns,
        )
        self.statusBar().showMessage("錄製與匯出完成", 8000)
        self.refresh_sessions()
        self.refresh_ai_sessions()
        self.refresh_recovery_sessions()

    @Slot(str)
    def _recording_failed(self, message: str) -> None:
        self.logger.error("Recording failed; recovery data retained")
        self.recording_status_label.setText("錄製未完成：" + message)
        self.statusBar().showMessage("錄製工作失敗；復原資料已保留")
        self.refresh_sessions()
        self.refresh_recovery_sessions()

    @Slot()
    def _recording_finished(self) -> None:
        worker = self._recording_worker
        if worker is not None:
            self._workers.discard(worker)
        self._recording_worker = None
        self._recording_workflow = None
        self._recording_signals = None
        self.start_recording_button.setEnabled(True)
        self.stop_recording_button.setEnabled(False)
        self.recording_title_edit.setEnabled(True)
        self.start_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self._update_model_action_state()
        self._load_recovery_session(self.recovery_session_combo.currentIndex())
        self.audio_level.setValue(0)
        self._update_session_action_state()
        self._load_ai_session(self.ai_session_combo.currentIndex())
        if self._close_after_recording:
            self._close_after_recording = False
            QTimer.singleShot(0, self.close)

    @Slot()
    def _open_recording_output(self) -> None:
        if self._recording_output_directory is None:
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._recording_output_directory))
        )

    def _summary_markdown(self, session_id: int) -> str:
        session = self.session_store.get_session(session_id)
        chapters = self.session_store.get_chapters(session_id)
        if not session.overall_summary or not chapters:
            return ""
        lines = [
            f"# {session.title}",
            "",
            "## 完整摘要",
            "",
            session.overall_summary,
            "",
            "## 章節導覽",
            "",
        ]
        for chapter in chapters:
            lines.extend(
                [
                    f"### {format_duration(chapter.start_time)} {chapter.title}",
                    "",
                    f"時間：{format_duration(chapter.start_time)}–"
                    f"{format_duration(chapter.end_time)}",
                    "",
                    chapter.summary,
                    "",
                ]
            )
            lines.extend(f"- {point}" for point in chapter.key_points)
            lines.append("")
        return "\n".join(lines)

    @Slot()
    def refresh_ai_sessions(self) -> None:
        selected = self.ai_session_combo.currentData()
        try:
            sessions = self.session_store.list_sessions()[:100]
            available = [
                session
                for session in sessions
                if session.status not in {"created", "capturing", "draining_queue"}
                and bool(self.session_store.get_transcript_segments(session.id))
            ]
        except DatabaseError as exc:
            self.ai_status_label.setText(f"無法讀取工作階段：{exc}")
            return
        self.ai_session_combo.blockSignals(True)
        self.ai_session_combo.clear()
        for session in available:
            suffix = " · 已有摘要" if session.overall_summary else ""
            self.ai_session_combo.addItem(
                f"#{session.id}　{session.title}　"
                f"({format_duration(session.duration_seconds)}){suffix}",
                session.id,
            )
        if selected is not None:
            index = self.ai_session_combo.findData(selected)
            if index >= 0:
                self.ai_session_combo.setCurrentIndex(index)
        self.ai_session_combo.blockSignals(False)
        self._load_ai_session(self.ai_session_combo.currentIndex())

    @Slot(int)
    def _load_ai_session(self, _index: int = -1) -> None:
        session_id = self.ai_session_combo.currentData()
        self._ai_summary_path = None
        self._ai_output_directory = None
        self.ai_open_summary_button.setEnabled(False)
        self.ai_open_output_button.setEnabled(False)
        if session_id is None:
            self.ai_summary_preview.clear()
            self.ai_status_label.setText("目前沒有可分析的逐字稿工作階段。")
            self.ai_session_info_label.setText(
                f"模型：OpenAI / {DEFAULT_OPENAI_MODEL}"
            )
            self.ai_start_button.setEnabled(False)
            return
        try:
            session = self.session_store.get_session(int(session_id))
            chapters = self.session_store.get_chapters(int(session_id))
        except DatabaseError as exc:
            self.ai_status_label.setText(f"工作階段讀取失敗：{exc}")
            self.ai_start_button.setEnabled(False)
            return
        provider_text = (
            f"{session.ai_provider}/{session.ai_model}"
            if session.ai_provider and session.ai_model
            else f"OpenAI/{DEFAULT_OPENAI_MODEL}"
        )
        self.ai_session_info_label.setText(
            f"工作階段 #{session.id}　·　{format_duration(session.duration_seconds)}　·　"
            f"模型 {provider_text}　·　只傳送逐字稿，不傳音訊"
        )
        if session.exported_path:
            self._ai_output_directory = Path(session.exported_path)
            self.ai_open_output_button.setEnabled(
                self._ai_output_directory.is_dir()
            )
            summary_path = self._ai_output_directory / "summary.md"
            if summary_path.is_file():
                self._ai_summary_path = summary_path
                self.ai_open_summary_button.setEnabled(True)
        if session.overall_summary and chapters:
            self.ai_summary_preview.setMarkdown(self._summary_markdown(session.id))
            self.ai_status_label.setText(
                f"已有 {len(chapters)} 個章節；重新分析仍可能產生 API 費用。"
            )
            self.ai_start_button.setText("重新分析（可能產生費用）")
            self.ai_progress_bar.setValue(100)
        else:
            self.ai_summary_preview.clear()
            self.ai_status_label.setText("尚未產生 AI 章節摘要。")
            self.ai_start_button.setText("產生 AI 章節摘要")
            self.ai_progress_bar.setValue(0)
        self.ai_start_button.setEnabled(
            self._recording_workflow is None and self._ai_workflow is None
        )

    @Slot()
    def start_ai_analysis(self) -> None:
        if self._ai_workflow is not None:
            return
        if self._maintenance_busy():
            QMessageBox.warning(
                self,
                "模型或復原工作正在進行",
                "請先完成目前的背景工作再執行 AI 分析。",
            )
            return
        if self._recording_workflow is not None:
            QMessageBox.warning(self, "錄製正在進行", "請先完成錄製再執行 AI 分析。")
            return
        session_id = self.ai_session_combo.currentData()
        if session_id is None:
            return
        try:
            api_key = self.secret_store.get(OPENAI_SECRET)
            session = self.session_store.get_session(int(session_id))
        except (CredentialError, DatabaseError) as exc:
            QMessageBox.critical(self, "無法開始 AI 分析", str(exc))
            return
        if not api_key:
            QMessageBox.warning(
                self,
                "尚未設定 OpenAI API Key",
                "請先到「錄製與儲存設定」保存 OpenAI API Key。",
            )
            self.tabs.setCurrentIndex(self.settings_tab_index)
            return
        existing = bool(session.overall_summary)
        question = (
            f"將使用 {DEFAULT_OPENAI_MODEL} 分析工作階段 #{session.id}。\n\n"
            "只傳送逐字稿文字、說話者標籤與時間戳，不傳送音訊。\n"
            "這會使用 OpenAI API 額度；成功的文字批次會沿用快取。"
        )
        if existing:
            question += "\n\n既有摘要會保留到新的完整結果成功產生為止。"
        answer = QMessageBox.question(self, "確認使用 OpenAI API", question)
        if answer != QMessageBox.StandardButton.Yes:
            return

        signals = AIAnalysisSignals()
        signals.status.connect(self._ai_status_changed)
        signals.progress.connect(self._ai_progress_changed)
        workflow = AIAnalysisWorkflow(
            self.paths,
            AIAnalysisRequest(session_id=int(session_id)),
            callbacks=AIAnalysisCallbacks(
                on_status=signals.status.emit,
                on_progress=signals.progress.emit,
            ),
            secret_store=self.secret_store,
        )
        worker = FunctionWorker(workflow.run)
        worker.signals.result.connect(self._ai_analysis_completed)
        worker.signals.error.connect(self._ai_analysis_failed)
        worker.signals.finished.connect(self._ai_analysis_finished)
        self._ai_workflow = workflow
        self._ai_worker = worker
        self._ai_signals = signals
        self._workers.add(worker)
        self._update_session_action_state()
        self.logger.info("AI analysis started: session_id=%s", session.id)
        self.ai_session_combo.setEnabled(False)
        self.ai_start_button.setEnabled(False)
        self.ai_cancel_button.setEnabled(True)
        self.start_recording_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.ai_progress_bar.setValue(1)
        self.ai_status_label.setText("正在準備 AI 分析…")
        self._update_model_action_state()
        self._load_recovery_session(self.recovery_session_combo.currentIndex())
        self.thread_pool.start(worker)

    @Slot()
    def cancel_ai_analysis(self) -> None:
        if self._ai_workflow is None:
            return
        self._ai_workflow.request_cancel()
        self.logger.info("AI analysis cancellation requested")
        self.ai_cancel_button.setEnabled(False)
        self.ai_status_label.setText(
            "已要求停止；目前 API 請求完成後會停止，成功批次仍保留快取。"
        )

    @Slot(str)
    def _ai_status_changed(self, message: str) -> None:
        self.ai_status_label.setText(message)
        self.statusBar().showMessage(message)

    @Slot(object)
    def _ai_progress_changed(self, progress: object) -> None:
        stage = str(getattr(progress, "stage", ""))
        completed = int(getattr(progress, "completed_batches", 0))
        total = max(1, int(getattr(progress, "total_batches", 1)))
        reused = int(getattr(progress, "reused_batches", 0))
        calls = int(getattr(progress, "provider_calls", 0))
        message = str(getattr(progress, "message", "AI 分析中…"))
        if stage == "batches":
            percent = round(80 * completed / total)
        elif stage == "consolidating":
            percent = 86
        elif stage == "summarizing":
            percent = 94
        else:
            percent = 98
        self.ai_progress_bar.setValue(max(1, min(98, percent)))
        self.ai_status_label.setText(
            f"{message}　批次 {completed}/{total} · 快取 {reused} · API 呼叫 {calls}"
        )

    @Slot(object)
    def _ai_analysis_completed(self, outcome: object) -> None:
        if not isinstance(outcome, AIAnalysisOutcome):
            self._ai_analysis_failed("AI 分析完成，但收到無法辨識的結果。")
            return
        if outcome.cancelled:
            self.logger.info("AI analysis cancelled")
            self.ai_status_label.setText("AI 分析已停止；成功的文字批次快取已保留。")
            self.refresh_sessions()
            return
        if outcome.analysis is None or outcome.summary_path is None:
            self._ai_analysis_failed("AI 分析沒有產生完整結果。")
            return
        self._ai_summary_path = outcome.summary_path
        self._ai_output_directory = outcome.summary_path.parent
        self._load_ai_session(self.ai_session_combo.currentIndex())
        analysis = outcome.analysis
        self.ai_progress_bar.setValue(100)
        self.ai_status_label.setText(
            f"完成：{len(analysis.chapters)} 個章節 · "
            f"文字批次 {analysis.batches_total} · 沿用快取 {analysis.batches_reused} · "
            f"本次 API 呼叫 {analysis.provider_calls}"
        )
        self.ai_open_summary_button.setEnabled(True)
        self.ai_open_output_button.setEnabled(True)
        self.logger.info(
            "AI analysis completed: chapters=%s batches=%s reused=%s provider_calls=%s",
            len(analysis.chapters),
            analysis.batches_total,
            analysis.batches_reused,
            analysis.provider_calls,
        )
        self.refresh_sessions()
        self.refresh_ai_sessions()
        self.refresh_recovery_sessions()
        self.statusBar().showMessage("AI 章節摘要完成", 8000)

    @Slot(str)
    def _ai_analysis_failed(self, message: str) -> None:
        self.logger.error("AI analysis failed; transcript and cache retained")
        self.ai_status_label.setText("AI 分析未完成：" + message)
        self.statusBar().showMessage("AI 分析未完成；逐字稿與成功快取已保留")
        self.refresh_sessions()
        self.refresh_recovery_sessions()

    @Slot()
    def _ai_analysis_finished(self) -> None:
        worker = self._ai_worker
        if worker is not None:
            self._workers.discard(worker)
        self._ai_worker = None
        self._ai_workflow = None
        self._ai_signals = None
        self.ai_session_combo.setEnabled(True)
        self.ai_start_button.setEnabled(
            self.ai_session_combo.currentData() is not None
        )
        self.ai_cancel_button.setEnabled(False)
        self.start_recording_button.setEnabled(True)
        self.start_button.setEnabled(True)
        self._update_model_action_state()
        self._load_recovery_session(self.recovery_session_combo.currentIndex())
        self._update_session_action_state()
        if self._close_after_ai:
            self._close_after_ai = False
            QTimer.singleShot(0, self.close)

    @Slot()
    def _open_ai_output(self) -> None:
        if self._ai_output_directory is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._ai_output_directory))
            )

    @Slot()
    def _open_ai_summary(self) -> None:
        if self._ai_summary_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._ai_summary_path)))

    def _selected_session_id(self) -> int | None:
        selected_rows = self.sessions_table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        value = self.sessions_table.item(selected_rows[0].row(), 0).data(
            Qt.ItemDataRole.UserRole
        )
        return int(value) if value is not None else None

    @staticmethod
    def _session_export_is_complete(exported_path: str | None) -> bool:
        if not exported_path:
            return False
        directory = Path(exported_path).expanduser().resolve()
        return all(
            (directory / filename).is_file()
            for filename in ("transcript.txt", "transcript.json", "subtitles.srt")
        )

    @Slot()
    def _update_session_action_state(self) -> None:
        session_id = self._selected_session_id()
        busy = (
            self._recording_workflow is not None
            or self._ai_workflow is not None
            or self._maintenance_busy()
            or self._session_action_worker is not None
        )
        if session_id is None:
            self.session_action_hint.setText("選取工作階段後可開啟、重新匯出或刪除。")
            self.session_open_button.setEnabled(False)
            self.session_reexport_button.setEnabled(False)
            self.session_delete_button.setEnabled(False)
            return
        try:
            session = self.session_store.get_session(session_id)
            segment_count = len(
                self.session_store.get_transcript_segments(session_id)
            )
        except DatabaseError:
            self.session_action_hint.setText("無法讀取選取的工作階段。")
            self.session_open_button.setEnabled(False)
            self.session_reexport_button.setEnabled(False)
            self.session_delete_button.setEnabled(False)
            return
        export_ready = self._session_export_is_complete(session.exported_path)
        self.session_action_hint.setText(
            f"工作階段 #{session.id} · {segment_count} 段逐字稿 · "
            + ("匯出檔完整" if export_ready else "需要重新匯出")
        )
        self.session_open_button.setEnabled(not busy and export_ready)
        self.session_reexport_button.setEnabled(not busy)
        self.session_delete_button.setEnabled(not busy)

    @Slot()
    def _open_selected_session_output(self) -> None:
        session_id = self._selected_session_id()
        if session_id is None:
            return
        try:
            session = self.session_store.get_session(session_id)
        except DatabaseError as exc:
            QMessageBox.warning(self, "無法開啟輸出", str(exc))
            return
        if not self._session_export_is_complete(session.exported_path):
            QMessageBox.information(
                self,
                "匯出檔不存在",
                "這個工作階段的逐字稿仍保存在 SQLite，請按「重新匯出」。",
            )
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(Path(session.exported_path or "").resolve()))
        )

    @Slot()
    def _reexport_selected_session(self) -> None:
        if self._session_action_worker is not None:
            return
        session_id = self._selected_session_id()
        if session_id is None:
            return
        try:
            self.session_store.get_session(session_id)
        except DatabaseError as exc:
            QMessageBox.warning(self, "無法重新匯出", str(exc))
            return

        def perform_export() -> ExportResult:
            return export_session(
                self.session_store,
                session_id,
                output_root=Path(self.settings.export_directory),
            )

        worker = FunctionWorker(perform_export)
        worker.signals.result.connect(self._session_reexport_completed)
        worker.signals.error.connect(self._session_reexport_failed)
        worker.signals.finished.connect(self._session_action_finished)
        self._session_action_worker = worker
        self._session_action_session_id = session_id
        self._workers.add(worker)
        self.session_action_hint.setText(f"工作階段 #{session_id} 正在重新匯出…")
        self._update_session_action_state()
        self.logger.info("Session re-export started: session_id=%s", session_id)
        self.thread_pool.start(worker)

    @Slot(object)
    def _session_reexport_completed(self, result: object) -> None:
        if not isinstance(result, ExportResult):
            self._session_reexport_failed("重新匯出完成，但收到無法辨識的結果。")
            return
        session_id = self._session_action_session_id
        if session_id is not None:
            try:
                session = self.session_store.get_session(session_id)
                pending = self.session_store.list_pending_chunks(session_id)
                speaker_ready = session.diarization_status in {
                    None,
                    "completed",
                    "skipped",
                }
                if not pending and speaker_ready:
                    self.session_store.update_status(
                        session_id,
                        "completed",
                        error_message=None,
                    )
            except DatabaseError:
                pass
        self.logger.info("Session re-export completed: session_id=%s", session_id)
        self.statusBar().showMessage("逐字稿重新匯出完成", 7000)

    @Slot(str)
    def _session_reexport_failed(self, message: str) -> None:
        self.logger.error(
            "Session re-export failed: session_id=%s",
            self._session_action_session_id,
        )
        self.session_action_hint.setText("重新匯出失敗；SQLite 逐字稿未受影響。")
        QMessageBox.warning(self, "重新匯出失敗", message)

    @Slot()
    def _session_action_finished(self) -> None:
        worker = self._session_action_worker
        if worker is not None:
            self._workers.discard(worker)
        self._session_action_worker = None
        self._session_action_session_id = None
        self.refresh_sessions()
        self.refresh_ai_sessions()
        self.refresh_recovery_sessions()

    @staticmethod
    def _is_direct_child(path: Path, parent: Path) -> bool:
        return path.resolve().parent == parent.resolve()

    def _delete_session_files(self, session_id: int) -> list[str]:
        session = self.session_store.get_session(session_id)
        export_path_to_remove: Path | None = None
        chunk_paths_to_remove: list[Path] = []
        report_path_to_remove: Path | None = None
        removed: list[str] = []
        if session.exported_path:
            export_path = Path(session.exported_path).expanduser().resolve()
            export_root = Path(session.export_directory).expanduser().resolve()
            if export_path.exists():
                if not self._is_direct_child(export_path, export_root):
                    raise OSError(
                        "匯出資料夾不在該工作階段的匯出根目錄內，為避免誤刪已停止。"
                    )
                if not export_path.is_dir():
                    raise OSError("工作階段匯出位置不是資料夾，為避免誤刪已停止。")
                export_path_to_remove = export_path

        for chunk in self.session_store.list_session_chunks(session_id):
            chunk_path = Path(chunk.path).expanduser().resolve()
            if chunk_path.exists():
                if self.paths.pending_audio.resolve() not in chunk_path.parents:
                    raise OSError("待處理音訊位於非預期位置，為避免誤刪已停止。")
                if not chunk_path.is_file():
                    raise OSError("待處理音訊不是一般檔案，為避免誤刪已停止。")
                chunk_paths_to_remove.append(chunk_path)

        if session.stability_report_path:
            report_path = Path(session.stability_report_path).expanduser().resolve()
            if report_path.exists():
                if self.paths.diagnostics.resolve() not in report_path.parents:
                    raise OSError("診斷報告位於非預期位置，為避免誤刪已停止。")
                if not report_path.is_file():
                    raise OSError("診斷報告不是一般檔案，為避免誤刪已停止。")
                report_path_to_remove = report_path

        diarization_audio = (
            self.paths.cache / "Diarization" / f"session_{session_id}_diarization.wav"
        ).resolve()
        if diarization_audio.exists() and not diarization_audio.is_file():
            raise OSError("說話者分析音訊不是一般檔案，為避免誤刪已停止。")

        if export_path_to_remove is not None:
            shutil.rmtree(export_path_to_remove)
            removed.append(str(export_path_to_remove))
        for chunk_path in chunk_paths_to_remove:
            chunk_path.unlink()
            removed.append(str(chunk_path))
        if report_path_to_remove is not None:
            report_path_to_remove.unlink()
            removed.append(str(report_path_to_remove))
        if diarization_audio.is_file():
            diarization_audio.unlink()
            removed.append(str(diarization_audio))

        spool_directory = (
            self.paths.pending_audio / f"session_{session_id}"
        ).resolve()
        if self._is_direct_child(spool_directory, self.paths.pending_audio):
            try:
                spool_directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # Unknown files are deliberately retained rather than recursively deleted.
                pass
        return removed

    @Slot()
    def _delete_selected_session(self) -> None:
        if (
            self._recording_workflow is not None
            or self._ai_workflow is not None
            or self._maintenance_busy()
            or self._session_action_worker is not None
        ):
            return
        session_id = self._selected_session_id()
        if session_id is None:
            return
        try:
            session = self.session_store.get_session(session_id)
        except DatabaseError as exc:
            QMessageBox.warning(self, "無法刪除工作階段", str(exc))
            return
        choices = (
            "只刪除工作階段記錄（保留匯出檔案）",
            "刪除記錄與這個工作階段的本機檔案",
        )
        choice, accepted = QInputDialog.getItem(
            self,
            "刪除工作階段",
            f"工作階段 #{session.id}：{session.title}\n請選擇刪除範圍：",
            choices,
            0,
            False,
        )
        if not accepted:
            return
        delete_files = choice == choices[1]
        detail = (
            "將刪除 SQLite 記錄，以及此工作階段的匯出資料夾、待處理音訊與診斷報告。"
            if delete_files
            else "只刪除 SQLite 工作階段記錄；已匯出的檔案會保留。"
        )
        if (
            QMessageBox.question(
                self,
                "確認永久刪除",
                f"{detail}\n\n這項操作無法從程式內復原。確定繼續嗎？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            removed = self._delete_session_files(session_id) if delete_files else []
            self.session_store.delete_session(session_id)
        except (DatabaseError, OSError) as exc:
            self.logger.error(
                "Session deletion failed: session_id=%s type=%s",
                session_id,
                type(exc).__name__,
            )
            QMessageBox.warning(self, "刪除未完成", str(exc))
            return
        self.logger.info(
            "Session deleted: session_id=%s files_removed=%s",
            session_id,
            len(removed),
        )
        self.statusBar().showMessage(f"工作階段 #{session_id} 已刪除", 7000)
        self.refresh_sessions()
        self.refresh_ai_sessions()
        self.refresh_recovery_sessions()

    @Slot()
    def refresh_sessions(self) -> None:
        selected_session_id = self._selected_session_id()
        try:
            sessions = self.session_store.list_sessions()[:20]
        except DatabaseError as exc:
            self.statusBar().showMessage(f"工作階段讀取失敗：{exc}")
            return
        self.sessions_table.setRowCount(len(sessions))
        selected_row = -1
        for row_index, session in enumerate(sessions):
            runtime = session.resolved_device or session.requested_device
            if session.compute_type:
                runtime = f"{runtime}/{session.compute_type}"
            export_missing = (
                session.status == "completed"
                and not self._session_export_is_complete(session.exported_path)
            )
            status_text = (
                "需重新匯出"
                if export_missing
                else _STATUS_LABELS.get(session.status, session.status)
            )
            values = (
                f"#{session.id}",
                session.title,
                status_text,
                format_duration(session.duration_seconds),
                runtime,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, session.id)
                if column in {0, 2, 3, 4}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 2:
                    status_colors = {
                        "完成": "#57D39B",
                        "需重新匯出": "#F2B84B",
                        "需要處理": "#F17881",
                        "錄製中": "#67A7FF",
                    }
                    item.setForeground(
                        QBrush(QColor(status_colors.get(status_text, "#A9B7CA")))
                    )
                    if session.error_message:
                        item.setToolTip(session.error_message)
                self.sessions_table.setItem(row_index, column, item)
            self.sessions_table.setRowHeight(row_index, 38)
            if session.id == selected_session_id:
                selected_row = row_index
        if selected_row >= 0:
            self.sessions_table.selectRow(selected_row)
        else:
            self.sessions_table.clearSelection()
        self._update_session_action_state()

    @Slot()
    def refresh_diagnostics(self) -> None:
        if self._recording_workflow is not None or self._maintenance_busy():
            self.statusBar().showMessage("背景工作期間暫不執行硬體診斷。", 5000)
            return
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("檢測中…")
        self.statusBar().showMessage("正在背景檢測硬體與音訊裝置…")
        worker = FunctionWorker(lambda: collect_diagnostics(self.paths))
        self._workers.add(worker)
        worker.signals.result.connect(self._apply_diagnostics)
        worker.signals.error.connect(self._diagnostics_failed)
        worker.signals.finished.connect(lambda: self._worker_finished(worker))
        self.thread_pool.start(worker)

    def _worker_finished(self, worker: FunctionWorker) -> None:
        self._workers.discard(worker)
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("重新檢測")

    @Slot(str)
    def _diagnostics_failed(self, message: str) -> None:
        self.logger.error("Hardware diagnostics failed")
        self.hardware_card.set_content("檢測失敗", "請查看診斷頁面")
        self.audio_card.set_content("檢測失敗", "可稍後重新檢測")
        self.diagnostics_text.setPlainText(message)
        self.statusBar().showMessage("硬體診斷失敗")

    @Slot(object)
    def _apply_diagnostics(self, report: dict[str, Any]) -> None:
        self._last_diagnostics_report = report
        self.logger.info("Hardware diagnostics completed")
        gpus = report.get("gpus") or []
        gpu_names = [str(item.get("name") or "未知 GPU") for item in gpus]
        gpu_text = "、".join(gpu_names) if gpu_names else "未偵測到獨立 GPU"
        memory = report.get("memory") or {}
        memory_text = _gib_text(memory.get("total_gib"))
        cpu_name = str((report.get("cpu") or {}).get("name") or "未知 CPU")
        self.hardware_card.set_content(gpu_text, f"{cpu_name} · RAM {memory_text}")

        audio_devices = report.get("audio_controllers") or []
        self.audio_card.set_content(
            f"偵測到 {len(audio_devices)} 個控制器",
            "系統音訊將使用預設 WASAPI Loopback",
        )

        warnings_list = [str(item) for item in report.get("warnings") or []]
        lines = [
            f"作業系統：{(report.get('operating_system') or {}).get('system', '未知')} "
            f"{(report.get('operating_system') or {}).get('release', '')}",
            f"Python：{(report.get('python') or {}).get('version', '未知')}",
            f"CPU：{cpu_name}",
            f"記憶體：{memory_text}",
            "GPU：" + gpu_text,
            "",
            "音訊控制器：",
        ]
        lines.extend(
            f"- {item.get('name', '未知')}" for item in audio_devices
        )
        if not audio_devices:
            lines.append("- 未偵測到")
        lines.extend(["", "診斷提示："])
        lines.extend(f"- {item}" for item in warnings_list)
        if not warnings_list:
            lines.append("- 無")
        lines.extend(
            [
                "",
                "隱私：診斷不包含逐字稿、API Key 或完整環境變數。",
                f"資料目錄：{self.paths.root}",
            ]
        )
        self.diagnostics_text.setPlainText("\n".join(lines))
        self.statusBar().showMessage("硬體診斷完成", 5000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._recording_workflow is not None:
            answer = QMessageBox.question(
                self,
                "錄製仍在進行",
                "關閉前需要停止擷取並完成已錄到的逐字稿。現在停止嗎？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_recording = True
            self.stop_recording()
            event.ignore()
            return
        if self._ai_workflow is not None:
            answer = QMessageBox.question(
                self,
                "AI 分析仍在進行",
                "目前 API 請求完成後才能安全停止。要停止分析並在完成收尾後關閉嗎？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_ai = True
            self.cancel_ai_analysis()
            event.ignore()
            return
        if self._maintenance_worker is not None:
            if self._maintenance_task == "recovery":
                question = (
                    "目前 Chunk 完成後才能安全停止。要停止復原並在收尾後關閉嗎？"
                )
            else:
                question = (
                    "模型下載正在進行，無法在檔案寫入途中強制中止。"
                    "要在下載完成或失敗後自動關閉嗎？"
                )
            answer = QMessageBox.question(self, "背景工作仍在進行", question)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_maintenance = True
            if self._recovery_workflow is not None:
                self.cancel_session_recovery()
            event.ignore()
            return
        if self._workers:
            self.thread_pool.waitForDone(3000)
        event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0b1018; color: #e7edf7; }
            QWidget { font-family: "Segoe UI Variable", "Microsoft JhengHei UI",
                "Segoe UI", "Microsoft JhengHei"; font-size: 14px; }
            QLabel { background: transparent; }
            QLabel#pageTitle { color: #f4f7fc; font-size: 29px; font-weight: 700; }
            QLabel#pageSubtitle, QLabel#mutedText, QLabel#cardDetail { color: #8f9db2; }
            QLabel#versionBadge { color: #9bc1ff; background: #14213a;
                border: 1px solid #29456f; border-radius: 14px; padding: 7px 12px; }
            QLabel#sectionTitle { color: #f0f4fa; font-size: 18px; font-weight: 650; }
            QLabel#recordingStatus { font-size: 16px; font-weight: 650; }
            QLabel#recordingClock { font-size: 24px; font-weight: 700; color: #72a9ff; }
            QLabel#cardTitle { color: #8f9db2; font-size: 12px; font-weight: 650; }
            QLabel#cardValue { color: #f1f5fb; font-size: 17px; font-weight: 650; }
            QLabel#secretStatus { background: #111a28; border: 1px solid #24344b;
                border-radius: 9px; padding: 12px; color: #b9c5d6; }
            QFrame#infoCard { background: #111925; border: 1px solid #222f42;
                border-radius: 12px; min-height: 112px; }
            QFrame#sectionCard, QFrame#recordingPanel { background: #101925;
                border: 1px solid #222f42; border-radius: 11px; }
            QFrame#recordingPanel { padding: 10px; }
            QScrollArea { border: 0; background: transparent; }
            QTabWidget::pane { border: 0; }
            QTabBar::tab { color: #8998ae; background: transparent; border: 0;
                border-radius: 8px; padding: 10px 17px; margin: 1px 3px 4px 0; }
            QTabBar::tab:hover { color: #c7d5e8; background: #101a29; }
            QTabBar::tab:selected { color: #eef5ff; background: #15233a;
                border-bottom: 2px solid #5f9dff; }
            QPushButton { color: #dbe5f3; background: #182436; border: 1px solid #2b3c55;
                border-radius: 8px; padding: 8px 15px; min-height: 20px; }
            QPushButton:hover { background: #21314a; border-color: #3c5577; }
            QPushButton:pressed { background: #132036; }
            QPushButton:focus { border: 1px solid #72a9ff; }
            QPushButton:disabled { color: #627086; background: #101722;
                border-color: #1d2837; }
            QPushButton#primaryButton { background: #3f82e6; border-color: #5799f5;
                color: #ffffff; font-weight: 650; }
            QPushButton#primaryButton:hover { background: #5293f2; border-color: #72abfa; }
            QPushButton#primaryButton:pressed { background: #3272ce; }
            QPushButton#primaryButton:disabled { color: #657389; background: #121a25;
                border-color: #1e2938; }
            QPushButton#dangerButton { color: #f2a3aa; background: transparent;
                border-color: #53313a; }
            QPushButton#dangerButton:hover { color: #fff0f1; background: #4a222c;
                border-color: #a94b5b; }
            QLineEdit, QComboBox, QTextEdit, QTableWidget {
                color: #e5edf8; background: #0f1723; border: 1px solid #27364b;
                border-radius: 8px; selection-background-color: #376fc0; padding: 8px; }
            QLineEdit:hover, QComboBox:hover, QTextEdit:hover { border-color: #354b68; }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border-color: #65a1ff; }
            QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled {
                color: #637187; background: #0d131d; border-color: #1b2635; }
            QComboBox::drop-down { border: 0; width: 28px; }
            QComboBox QAbstractItemView { color: #e5edf8; background: #111b29;
                border: 1px solid #30445f; selection-background-color: #274d82;
                outline: 0; padding: 5px; }
            QHeaderView::section { background: #162130; color: #9eacc0; border: 0;
                border-bottom: 1px solid #27374d; padding: 9px; font-weight: 650; }
            QTableWidget { alternate-background-color: #101a27; gridline-color: transparent; }
            QTableWidget::item { border-bottom: 1px solid #1e2a3a; padding: 7px; }
            QTableWidget::item:selected { color: #f3f7fd; background: #1c3f6d; }
            QProgressBar { background: #0b121c; border: 1px solid #26364b;
                border-radius: 6px; min-height: 14px; text-align: center; color: #e0e9f6; }
            QProgressBar::chunk { background: #39b982; border-radius: 5px; }
            QCheckBox { spacing: 9px; }
            QScrollBar:vertical { background: #0b1018; width: 11px; margin: 2px; }
            QScrollBar::handle:vertical { background: #2a3b52; border-radius: 4px;
                min-height: 28px; }
            QScrollBar::handle:vertical:hover { background: #3b5373; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QStatusBar { color: #8998ad; background: #090e15; border-top: 1px solid #182333; }
            QToolTip { color: #edf4ff; background: #172234; border: 1px solid #385170;
                padding: 6px; }
            QMessageBox { background: #101925; }
            """
        )
