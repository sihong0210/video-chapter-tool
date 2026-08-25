from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import AppPaths, AppSettings, SettingsError, SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def test_load_or_create_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            store = SettingsStore(paths)

            created = store.load_or_create()
            loaded = store.load()

            self.assertEqual(created, loaded)
            self.assertEqual(created.whisper_model, "small")
            self.assertEqual(created.compute_device, "auto")
            self.assertEqual(created.speaker_mode, "auto")
            self.assertEqual(created.speaker_count, 2)
            self.assertEqual(created.diarization_device, "auto")
            self.assertTrue(created.keep_display_on_during_recording)
            self.assertTrue(created.traditional_chinese_output)
            self.assertTrue(paths.settings_file.is_file())

    def test_schema_one_is_migrated_with_traditional_output_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            legacy = AppSettings.defaults(paths).to_dict()
            legacy["schema_version"] = 1
            del legacy["traditional_chinese_output"]
            paths.settings_file.write_text(
                json.dumps(legacy),
                encoding="utf-8",
            )

            loaded = SettingsStore(paths).load()

            self.assertEqual(loaded.schema_version, 5)
            self.assertTrue(loaded.traditional_chinese_output)
            self.assertEqual(loaded.speaker_mode, "auto")
            persisted = json.loads(paths.settings_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 5)

    def test_schema_two_is_migrated_with_speaker_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            legacy = AppSettings.defaults(paths).to_dict()
            legacy["schema_version"] = 2
            del legacy["speaker_mode"]
            del legacy["speaker_count"]
            del legacy["diarization_device"]
            paths.settings_file.write_text(json.dumps(legacy), encoding="utf-8")

            loaded = SettingsStore(paths).load()

            self.assertEqual(loaded.schema_version, 5)
            self.assertEqual(loaded.speaker_mode, "auto")
            self.assertEqual(loaded.speaker_count, 2)
            self.assertEqual(loaded.diarization_device, "auto")

    def test_schema_three_is_migrated_with_display_awake_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            legacy = AppSettings.defaults(paths).to_dict()
            legacy["schema_version"] = 3
            del legacy["keep_display_on_during_recording"]
            paths.settings_file.write_text(json.dumps(legacy), encoding="utf-8")

            loaded = SettingsStore(paths).load()

            self.assertEqual(loaded.schema_version, 5)
            self.assertTrue(loaded.keep_display_on_during_recording)

    def test_save_supports_custom_model_and_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            store = SettingsStore(paths)
            settings = AppSettings.defaults(paths)
            settings.model_directory = str(Path(temporary_directory) / "外接磁碟 模型")
            settings.export_directory = str(Path(temporary_directory) / "逐字稿 匯出")

            store.save(settings)

            self.assertEqual(store.load(), settings)
            temporary_files = list(paths.config.glob("*.tmp"))
            self.assertEqual(temporary_files, [])

    def test_default_managed_paths_are_persisted_relatively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(Path(temporary_directory) / "UserData")
            store = SettingsStore(paths)

            loaded = store.load_or_create()
            persisted = json.loads(paths.settings_file.read_text(encoding="utf-8"))

            self.assertEqual(persisted["model_directory"], "Models")
            self.assertEqual(persisted["export_directory"], "Exports")
            self.assertEqual(loaded.model_directory, str(paths.models))
            self.assertEqual(loaded.export_directory, str(paths.exports))

    def test_relative_managed_paths_rebase_after_portable_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            original = AppPaths.from_root(base / "Original" / "UserData")
            SettingsStore(original).load_or_create()
            settings_json = original.settings_file.read_text(encoding="utf-8")

            moved = AppPaths.from_root(base / "Moved" / "UserData")
            moved.ensure()
            moved.settings_file.write_text(settings_json, encoding="utf-8")
            loaded = SettingsStore(moved).load()

            self.assertEqual(loaded.model_directory, str(moved.models))
            self.assertEqual(loaded.export_directory, str(moved.exports))

    def test_invalid_json_does_not_get_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            paths.settings_file.write_text("{not valid json", encoding="utf-8")

            with self.assertRaises(SettingsError):
                SettingsStore(paths).load_or_create()

            self.assertEqual(
                paths.settings_file.read_text(encoding="utf-8"),
                "{not valid json",
            )

    def test_missing_required_setting_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)
            paths.ensure()
            paths.settings_file.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )

            with self.assertRaises(SettingsError):
                SettingsStore(paths).load()


if __name__ == "__main__":
    unittest.main()
