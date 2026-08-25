from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.transcription.engine import (
    RuntimeInfo,
    WhisperEngine,
    _configure_windows_cuda_dll_search,
    cuda_preflight_error,
    runtime_candidates,
)
from app.transcription.transcriber import BasicTranscriber


class RuntimeSelectionTests(unittest.TestCase):
    def test_close_releases_native_model_reference(self) -> None:
        runtime = RuntimeInfo(
            requested_device="cuda",
            resolved_device="cuda",
            compute_type="float16",
            cpu_threads=4,
            model_load_seconds=0.1,
            fallback_reason=None,
        )
        engine = WhisperEngine(object(), runtime)

        engine.close()
        engine.close()

        self.assertIsNone(engine.model)

    @patch("app.transcription.engine._REGISTERED_DLL_DIRECTORIES", set())
    @patch("app.transcription.engine._DLL_DIRECTORY_HANDLES", [])
    @patch("app.transcription.engine.os.add_dll_directory")
    @patch("app.transcription.engine._windows_cuda_dll_directories")
    def test_configures_discovered_windows_dll_directories_once(
        self,
        mock_directories,
        mock_add_directory,
    ) -> None:
        mock_directories.return_value = (Path(r"C:\CUDA\bin"),)

        _configure_windows_cuda_dll_search()
        _configure_windows_cuda_dll_search()

        mock_add_directory.assert_called_once_with(r"C:\CUDA\bin")

    @patch("app.transcription.engine._cuda_device_count", return_value=1)
    def test_auto_prefers_cuda_then_cpu(self, _mock_count) -> None:
        candidates = runtime_candidates("auto")
        self.assertEqual(candidates[0].device, "cuda")
        self.assertEqual(candidates[0].compute_type, "float16")
        self.assertEqual(candidates[1].device, "cpu")
        self.assertEqual(candidates[1].compute_type, "int8")

    @patch("app.transcription.engine._cuda_device_count", return_value=0)
    def test_auto_uses_cpu_without_cuda(self, _mock_count) -> None:
        candidates = runtime_candidates("auto")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].device, "cpu")

    def test_amd_experimental_uses_cpu_candidate(self) -> None:
        candidates = runtime_candidates("amd_experimental")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].device, "cpu")

    @patch("app.transcription.engine.ctypes.WinDLL", side_effect=OSError)
    @patch("app.transcription.engine._cuda_device_count", return_value=1)
    def test_cuda_preflight_reports_missing_windows_libraries(
        self,
        _mock_count,
        _mock_win_dll,
    ) -> None:
        error = cuda_preflight_error()
        self.assertIsNotNone(error)
        self.assertIn("cublas64_12.dll", error)
        self.assertIn("cudnn64_9.dll", error)


class _FakeWhisperModel:
    def transcribe(self, _path: str, **_options):
        segments = iter(
            [
                SimpleNamespace(
                    start=1.2345,
                    end=2.3456,
                    text=" 測試逐字稿 ",
                    avg_logprob=-0.25,
                    no_speech_prob=0.01,
                    words=(
                        SimpleNamespace(
                            start=1.23,
                            end=1.8,
                            word=" 測試",
                            probability=0.9,
                        ),
                        SimpleNamespace(
                            start=1.8,
                            end=2.34,
                            word="逐字稿",
                            probability=0.8,
                        ),
                    ),
                ),
                SimpleNamespace(
                    start=2.4,
                    end=2.6,
                    text="   ",
                    avg_logprob=-1.0,
                    no_speech_prob=0.9,
                ),
            ]
        )
        info = SimpleNamespace(
            language="zh",
            language_probability=0.98,
            duration=5.0,
            duration_after_vad=3.0,
        )
        return segments, info


class BasicTranscriberTests(unittest.TestCase):
    def test_transcription_normalizes_timestamped_segments(self) -> None:
        runtime = RuntimeInfo(
            requested_device="cpu",
            resolved_device="cpu",
            compute_type="int8",
            cpu_threads=4,
            model_load_seconds=0.1,
            fallback_reason=None,
        )
        engine = SimpleNamespace(model=_FakeWhisperModel(), runtime=runtime)

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "audio.wav"
            source.write_bytes(b"fake")
            result = BasicTranscriber(engine).transcribe_file(
                source,
                language="zh",
                word_timestamps=True,
            )

        self.assertEqual(result.language, "zh")
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].start, 1.234)
        self.assertEqual(result.segments[0].end, 2.346)
        self.assertEqual(result.segments[0].text, "測試逐字稿")
        self.assertEqual(len(result.segments[0].words), 2)
        self.assertEqual(result.segments[0].words[0].text, " 測試")
        self.assertIsNotNone(result.real_time_factor)


if __name__ == "__main__":
    unittest.main()
