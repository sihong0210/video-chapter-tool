from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audio.chunking import AudioChunk
from app.database import SessionStore
from app.diarization import SpeakerTurn
from app.exporting import ExportError, export_session
from app.transcription.transcriber import TranscriptSegment


class TranscriptExportTests(unittest.TestCase):
    def test_exports_utf8_txt_json_and_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title='直播：新品/測試',
                platform="facebook",
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
            )
            chunk = AudioChunk(0, root / "chunk.wav", 0, 100, 10, 1, 2)
            chunk.path.write_bytes(b"audio")
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (TranscriptSegment(1.234, 2.345, "繁體逐字稿", -0.1, 0.01),),
            )
            store.replace_speaker_turns(
                session.id,
                (SpeakerTurn(1.0, 3.0, "speaker_A"),),
                provider_name="fake",
                model_name="fake-diarizer",
                requested_speaker_count=1,
            )

            result = export_session(store, session.id)

            self.assertTrue(result.txt_path.is_file())
            self.assertTrue(result.json_path.is_file())
            self.assertTrue(result.srt_path.is_file())
            self.assertIn("[00:00:01]", result.txt_path.read_text(encoding="utf-8"))
            self.assertIn("繁體逐字稿", result.json_path.read_text(encoding="utf-8"))
            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["segments"][0]["raw_text"], "繁體逐字稿")
            self.assertEqual(payload["segments"][0]["speaker_id"], "speaker_A")
            self.assertIn("說話者 A：", result.txt_path.read_text(encoding="utf-8"))
            self.assertIn(
                "00:00:01,234 --> 00:00:02,345",
                result.srt_path.read_text(encoding="utf-8"),
            )
            self.assertNotIn(":", result.directory.name)

            repeated = export_session(store, session.id)
            self.assertEqual(repeated.directory, result.directory)

    def test_failed_file_write_does_not_persist_exported_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="匯出失敗測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
            )

            with patch(
                "app.exporting.transcript._write_text_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaises(ExportError):
                    export_session(store, session.id)

            self.assertIsNone(store.get_session(session.id).exported_path)


if __name__ == "__main__":
    unittest.main()
