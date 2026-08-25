from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config.paths import (
    AppPaths,
    DATA_DIR_ENVIRONMENT_VARIABLE,
    PORTABLE_DATA_DIRECTORY_NAME,
)


class AppPathsTests(unittest.TestCase):
    def test_from_root_builds_expected_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)

            self.assertEqual(paths.models, paths.root / "Models")
            self.assertEqual(paths.diagnostics, paths.root / "Diagnostics")
            self.assertEqual(paths.pending_audio, paths.root / "PendingAudio")
            self.assertEqual(
                paths.stability_reports,
                paths.root / "Diagnostics" / "Stability",
            )
            self.assertEqual(paths.settings_file, paths.root / "Config" / "settings.json")
            self.assertEqual(
                paths.database_file,
                paths.root / "Database" / "video_chapter_tool.db",
            )

    def test_environment_override_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "Portable" / "VideoChapterTool.exe"
            paths = AppPaths.default(
                {
                    DATA_DIR_ENVIRONMENT_VARIABLE: temporary_directory,
                    "LOCALAPPDATA": str(Path(temporary_directory) / "ignored"),
                },
                frozen=True,
                executable=executable,
            )
            self.assertEqual(paths.root, Path(temporary_directory).resolve())

    def test_frozen_default_uses_userdata_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "Portable App" / "VideoChapterTool.exe"

            paths = AppPaths.default({}, frozen=True, executable=executable)

            self.assertEqual(
                paths.root,
                (executable.parent / PORTABLE_DATA_DIRECTORY_NAME).resolve(),
            )

    def test_internal_configured_paths_move_with_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            original = AppPaths.from_root(Path(temporary_directory) / "Original" / "UserData")
            moved = AppPaths.from_root(Path(temporary_directory) / "Moved" / "UserData")

            stored = original.serialize_configured_path(original.models)

            self.assertEqual(stored, "Models")
            self.assertEqual(moved.resolve_configured_path(stored), moved.models)

    def test_external_configured_paths_remain_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            paths = AppPaths.from_root(base / "Portable" / "UserData")
            external = (base / "External Models").resolve()

            stored = paths.serialize_configured_path(external)

            self.assertEqual(stored, str(external))

    def test_ensure_creates_all_managed_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "中文 路徑"
            paths = AppPaths.from_root(root)
            paths.ensure()

            for directory in paths.managed_directories():
                self.assertTrue(directory.is_dir(), directory)


if __name__ == "__main__":
    unittest.main()
