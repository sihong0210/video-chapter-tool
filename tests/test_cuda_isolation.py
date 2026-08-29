from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.diagnostics.cuda_isolation import _require_bundled_library


class CudaIsolationTests(unittest.TestCase):
    @patch(
        "app.diagnostics.cuda_isolation._loaded_windows_library_path",
        return_value=Path(r"C:\Portable\_internal\cublas64_12.dll"),
    )
    def test_accepts_library_inside_bundle(self, _mock_library_path) -> None:
        result = _require_bundled_library(
            "cublas64_12.dll",
            Path(r"C:\Portable\_internal"),
        )
        self.assertEqual(result.name, "cublas64_12.dll")

    @patch(
        "app.diagnostics.cuda_isolation._loaded_windows_library_path",
        return_value=Path(r"C:\Program Files\NVIDIA\cudnn64_9.dll"),
    )
    def test_rejects_library_outside_bundle(self, _mock_library_path) -> None:
        with self.assertRaisesRegex(RuntimeError, "outside the bundle"):
            _require_bundled_library(
                "cudnn64_9.dll",
                Path(r"C:\Portable\_internal"),
            )


if __name__ == "__main__":
    unittest.main()
