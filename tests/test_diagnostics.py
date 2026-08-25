from __future__ import annotations

import tempfile
import unittest

from app.config import AppPaths
from app.diagnostics.collector import collect_diagnostics
from app.diagnostics.windows_hardware import classify_gpu_vendor


class DiagnosticsTests(unittest.TestCase):
    def test_gpu_vendor_classification(self) -> None:
        examples = {
            "NVIDIA GeForce RTX 4070": "nvidia",
            "AMD Radeon RX 7800 XT": "amd",
            "Advanced Micro Devices Display Adapter": "amd",
            "Intel(R) Arc(TM) Graphics": "intel",
            "Generic Adapter": "unknown",
        }
        for name, expected_vendor in examples.items():
            with self.subTest(name=name):
                self.assertEqual(classify_gpu_vendor(name), expected_vendor)

    def test_diagnostics_declares_privacy_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = AppPaths.from_root(temporary_directory)

            report = collect_diagnostics(paths)

            self.assertFalse(report["privacy"]["contains_transcript"])
            self.assertFalse(report["privacy"]["contains_api_keys"])
            self.assertTrue(report["storage"]["app_cache_writable"])
            self.assertIn("recommended_for_mvp", report["acceleration"])


if __name__ == "__main__":
    unittest.main()

