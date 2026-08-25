from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from app.audio.chunking import AudioChunk
from app.config import AppPaths, SettingsStore
from app.database import DatabaseError, SessionStore
from app.exporting import export_session
from app.gui.main_window import MainWindow, format_duration
from app.infrastructure import (
    MemoryCredentialBackend,
    OPENAI_SECRET,
    SecretStore,
)
from app.models.manager import REQUIRED_MODEL_FILES
from app.transcription.transcriber import TranscriptSegment
from app.transcription import RecoveryProgress


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["gui-tests"])

    def test_formats_short_and_long_durations(self) -> None:
        self.assertEqual(format_duration(65), "01:05")
        self.assertEqual(format_duration(3661), "1:01:01")

    def _window(self, paths: AppPaths) -> MainWindow:
        return MainWindow(
            paths,
            auto_refresh=False,
            secret_store=SecretStore(MemoryCredentialBackend()),
        )

    def test_window_loads_existing_paths_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            settings = SettingsStore(paths).load_or_create()
            secret = "sk-test-secret-that-must-not-appear"
            with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
                window = self._window(paths)
                try:
                    self.assertEqual(
                        window.model_directory_edit.text(),
                        settings.model_directory,
                    )
                    self.assertIn(
                        "OpenAI API Key：已由環境變數設定",
                        window.secret_status_label.text(),
                    )
                    self.assertNotIn(secret, window.secret_status_label.text())
                finally:
                    window.close()

    def test_saves_paths_and_speaker_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = AppPaths.from_root(root / "data")
            window = self._window(paths)
            try:
                model_directory = root / "自訂 模型"
                export_directory = root / "自訂 匯出"
                window.model_directory_edit.setText(str(model_directory))
                window.export_directory_edit.setText(str(export_directory))
                window._set_combo_data(window.whisper_model_combo, "medium")
                window._set_combo_data(window.speaker_mode_combo, "fixed")
                window._set_combo_data(window.speaker_count_combo, 3)
                window._set_combo_data(window.diarization_device_combo, "cpu")
                window.keep_display_checkbox.setChecked(False)

                window.save_settings()

                saved = SettingsStore(paths).load()
                self.assertEqual(saved.speaker_mode, "fixed")
                self.assertEqual(saved.speaker_count, 3)
                self.assertEqual(saved.whisper_model, "medium")
                self.assertEqual(saved.diarization_device, "cpu")
                self.assertFalse(saved.keep_display_on_during_recording)
                self.assertTrue(model_directory.is_dir())
                self.assertTrue(export_directory.is_dir())
            finally:
                window.close()

    def test_recording_start_persists_visible_speaker_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            window = self._window(paths)
            try:
                window._set_combo_data(window.speaker_mode_combo, "fixed")
                window._set_combo_data(window.speaker_count_combo, 4)
                with patch(
                    "app.gui.main_window.ModelManager.status",
                    return_value=type(
                        "Status",
                        (),
                        {"valid": False, "path": "missing"},
                    )(),
                ), patch.object(
                    window,
                    "_persist_form_settings",
                    wraps=window._persist_form_settings,
                ) as save:
                    with patch(
                        "app.gui.main_window.QMessageBox.warning",
                    ):
                        window.start_recording()

                save.assert_called_once_with(show_success=False)
                saved = SettingsStore(paths).load()
                self.assertEqual(saved.speaker_mode, "fixed")
                self.assertEqual(saved.speaker_count, 4)
            finally:
                window.close()

    def test_secret_is_saved_without_remaining_in_the_password_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            window = self._window(paths)
            try:
                secret = "sk-test-never-render-this-value"
                window.openai_key_edit.setText(secret)
                window._save_secret(OPENAI_SECRET, window.openai_key_edit)

                self.assertEqual(window.openai_key_edit.text(), "")
                self.assertNotIn(secret, window.secret_status_label.text())
                self.assertIn(
                    "Windows 憑證管理員",
                    window.secret_status_label.text(),
                )
                window._delete_secret(OPENAI_SECRET, confirm=False)
                self.assertIn("OpenAI API Key：未設定", window.secret_status_label.text())
            finally:
                window.close()

    def test_recording_page_defaults_to_manual_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            window = self._window(AppPaths.from_root(temporary_directory))
            try:
                self.assertTrue(window.start_recording_button.isEnabled())
                self.assertFalse(window.stop_recording_button.isEnabled())
                self.assertIn("Chunk", window.transcript_preview.placeholderText())
            finally:
                window.close()

    def test_ai_page_lists_transcript_and_previews_existing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            store = SessionStore(paths.database_file)
            session = store.create_session(
                title="AI 摘要介面測試",
                platform="test",
                source="system-audio",
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=paths.exports,
            )
            chunk = AudioChunk(
                0,
                paths.pending_audio / "ai_gui_test.wav",
                0,
                20,
                1,
                1,
                2,
            )
            chunk.path.parent.mkdir(parents=True, exist_ok=True)
            chunk.path.write_bytes(b"audio")
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (TranscriptSegment(0, 20, "這是一段測試逐字稿。", -0.1, 0.01),),
            )
            store.update_status(session.id, "completed", duration_seconds=20)
            store.replace_ai_result(
                session.id,
                overall_summary="這是完整摘要。",
                provider_name="openai",
                model_name="gpt-test",
                chapters=(
                    {
                        "start": 0,
                        "end": 20,
                        "title": "測試章節",
                        "summary": "章節摘要。",
                        "key_points": ("重點一",),
                    },
                ),
            )

            window = self._window(paths)
            try:
                index = window.ai_session_combo.findData(session.id)
                self.assertGreaterEqual(index, 0)
                window.ai_session_combo.setCurrentIndex(index)
                window._load_ai_session(index)

                preview = window.ai_summary_preview.toPlainText()
                self.assertIn("這是完整摘要。", preview)
                self.assertIn("測試章節", preview)
                self.assertIn("重新分析", window.ai_start_button.text())
                self.assertTrue(window.ai_start_button.isEnabled())
                self.assertEqual(window.ai_progress_bar.value(), 100)
                self.assertGreaterEqual(window.ai_progress_bar.minimumWidth(), 150)
                self.assertEqual(
                    window.ai_progress_bar.alignment(),
                    Qt.AlignmentFlag.AlignCenter,
                )
            finally:
                window.close()

    def test_maintenance_page_lists_models_and_incomplete_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            small_path = paths.models / "small"
            small_path.mkdir(parents=True, exist_ok=True)
            for filename in REQUIRED_MODEL_FILES:
                (small_path / filename).write_bytes(b"test-model")
            store = SessionStore(paths.database_file)
            session = store.create_session(
                title="待復原介面測試",
                platform="test",
                source="system-audio",
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=paths.exports,
            )
            store.update_status(session.id, "capturing")

            window = self._window(paths)
            try:
                self.assertEqual(window.model_status_table.rowCount(), 2)
                self.assertGreaterEqual(window.model_status_table.minimumHeight(), 180)
                self.assertIn("模型資料夾", window.model_root_label.text())
                small_index = window.whisper_model_combo.findData("small")
                medium_index = window.whisper_model_combo.findData("medium")
                self.assertIn(
                    "可使用",
                    window.whisper_model_combo.itemText(small_index),
                )
                self.assertIn(
                    "尚未下載",
                    window.whisper_model_combo.itemText(medium_index),
                )
                model = window.whisper_model_combo.model()
                self.assertTrue(model.item(small_index).isEnabled())
                self.assertFalse(model.item(medium_index).isEnabled())
                index = window.recovery_session_combo.findData(session.id)
                self.assertGreaterEqual(index, 0)
                window.recovery_session_combo.setCurrentIndex(index)
                window._load_recovery_session(index)
                self.assertIn("沒有已保存內容", window.recovery_info_label.text())
                self.assertTrue(window.recovery_start_button.isEnabled())

                window._recovery_progress_changed(RecoveryProgress(1, 2, 3))
                self.assertEqual(window.recovery_progress_bar.value(), 40)
                self.assertIn("1/2", window.recovery_status_label.text())
            finally:
                window.close()

    def test_completed_session_with_missing_files_offers_reexport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            store = SessionStore(paths.database_file)
            session = store.create_session(
                title="缺少匯出檔",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=paths.exports,
            )
            store.update_status(session.id, "completed", duration_seconds=6)

            window = self._window(paths)
            try:
                window.sessions_table.selectRow(0)
                window._update_session_action_state()

                self.assertEqual(window.sessions_table.item(0, 2).text(), "需重新匯出")
                self.assertTrue(window.session_reexport_button.isEnabled())
                self.assertTrue(window.session_delete_button.isEnabled())
                self.assertFalse(window.session_open_button.isEnabled())
            finally:
                window.close()

    def test_delete_session_can_remove_record_and_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            store = SessionStore(paths.database_file)
            session = store.create_session(
                title="完整刪除測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=paths.exports,
            )
            exported = export_session(store, session.id)
            report = paths.stability_reports / f"session_{session.id}_stability.json"
            report.write_text("{}", encoding="utf-8")
            store.set_stability_report_path(session.id, report)
            store.update_status(session.id, "completed")

            window = self._window(paths)
            try:
                window.sessions_table.selectRow(0)
                with (
                    patch(
                        "app.gui.main_window.QInputDialog.getItem",
                        return_value=(
                            "刪除記錄與這個工作階段的本機檔案",
                            True,
                        ),
                    ),
                    patch(
                        "app.gui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes,
                    ),
                ):
                    window._delete_selected_session()

                with self.assertRaises(DatabaseError):
                    store.get_session(session.id)
                self.assertFalse(exported.directory.exists())
                self.assertFalse(report.exists())
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
