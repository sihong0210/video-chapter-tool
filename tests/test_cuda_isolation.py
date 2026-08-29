from __future__ import annotations

import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.diagnostics.cuda_isolation import (
    _require_bundled_library,
    _write_wav_prefix,
)


class CudaIsolationTests(unittest.TestCase):
    def test_wav_prefix_is_bounded(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.wav"
            destination = root / "prefix.wav"
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\0\0" * 32000)

            _write_wav_prefix(source, destination, 0.5)

            with wave.open(str(destination), "rb") as result:
                self.assertEqual(result.getnframes(), 8000)
                self.assertEqual(result.getframerate(), 16000)

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
