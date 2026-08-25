from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout

from app.config import AppPaths
from app.main import build_parser, main


class CommandLineTests(unittest.TestCase):
    def test_ai_export_command_accepts_session_id(self) -> None:
        args = build_parser().parse_args(["ai-export", "11"])

        self.assertEqual(args.command, "ai-export")
        self.assertEqual(args.session_id, 11)

    def test_ai_analyze_defaults_to_cost_optimized_openai_model(self) -> None:
        args = build_parser().parse_args(["ai-analyze", "11"])

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.model, "gpt-5.6-luna")
        self.assertEqual(args.language, "zh-TW")

    def test_stream_can_request_two_speakers(self) -> None:
        args = build_parser().parse_args(
            ["stream-transcribe", "--speakers", "2"]
        )

        self.assertEqual(args.speakers, "2")
        self.assertEqual(args.diarization_device, "auto")

    def test_speaker_analyze_defaults_to_auto_count(self) -> None:
        args = build_parser().parse_args(["speaker-analyze", "12"])

        self.assertEqual(args.session_id, 12)
        self.assertEqual(args.speakers, "auto")

    def test_speaker_diagnostics_accepts_json(self) -> None:
        args = build_parser().parse_args(["speaker-diagnostics", "--json"])

        self.assertEqual(args.command, "speaker-diagnostics")
        self.assertTrue(args.json)

    def test_speaker_model_download_defaults_to_auto_device(self) -> None:
        args = build_parser().parse_args(["speaker-model-download"])

        self.assertEqual(args.command, "speaker-model-download")
        self.assertEqual(args.device, "auto")

    def test_init_creates_settings_and_reports_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["--data-dir", temporary_directory, "init"])

            paths = AppPaths.from_root(temporary_directory)
            self.assertEqual(exit_code, 0)
            self.assertTrue(paths.settings_file.is_file())
            self.assertIn("初始化完成", output.getvalue())


if __name__ == "__main__":
    unittest.main()
