from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models.manager import (
    REQUIRED_MODEL_FILES,
    ModelManagementError,
    ModelManager,
)


def _create_fake_valid_model(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for filename in REQUIRED_MODEL_FILES:
        (path / filename).write_bytes(b"test-model-data")


class ModelManagerTests(unittest.TestCase):
    def test_missing_model_reports_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ModelManager(temporary_directory)
            status = manager.status("small")

            self.assertFalse(status.installed)
            self.assertFalse(status.valid)
            self.assertEqual(set(status.missing_files), set(REQUIRED_MODEL_FILES))

    def test_complete_model_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ModelManager(temporary_directory)
            _create_fake_valid_model(manager.model_path("small"))

            status = manager.status("small")

            self.assertTrue(status.installed)
            self.assertTrue(status.valid)
            self.assertGreater(status.size_bytes, 0)
            self.assertEqual(status.missing_files, ())

    def test_zero_byte_required_file_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ModelManager(temporary_directory)
            model_path = manager.model_path("small")
            _create_fake_valid_model(model_path)
            (model_path / "model.bin").write_bytes(b"")

            status = manager.status("small")

            self.assertFalse(status.valid)
            self.assertIn("model.bin", status.missing_files)

    def test_delete_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ModelManager(temporary_directory)
            _create_fake_valid_model(manager.model_path("small"))

            with self.assertRaises(ModelManagementError):
                manager.delete("small", confirmation="SMALL")

            self.assertTrue(manager.model_path("small").is_dir())

    def test_confirmed_delete_only_removes_named_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ModelManager(temporary_directory)
            _create_fake_valid_model(manager.model_path("small"))
            unrelated = Path(temporary_directory) / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")

            deleted = manager.delete("small", confirmation="small")

            self.assertTrue(deleted)
            self.assertFalse(manager.model_path("small").exists())
            self.assertTrue(unrelated.is_file())

    def test_unknown_model_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ModelManager(temporary_directory)
            with self.assertRaises(ModelManagementError):
                manager.status("unknown")


if __name__ == "__main__":
    unittest.main()

