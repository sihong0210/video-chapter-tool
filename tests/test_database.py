from __future__ import annotations

import tempfile
import unittest
import sqlite3
import shutil
from pathlib import Path

from app.audio.chunking import AudioChunk
from app.database import DatabaseError, SessionStore
from app.transcription.transcriber import TranscriptSegment


class SessionStoreTests(unittest.TestCase):
    def test_chunk_transcript_and_status_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="兩人直播測試",
                platform="facebook",
                source="測試來源",
                language="zh",
                model_name="small",
                requested_device="auto",
                export_directory=root / "exports",
            )
            chunk_path = root / "chunk.wav"
            chunk_path.write_bytes(b"audio")
            chunk = AudioChunk(0, chunk_path, 0, 480_000, 48_000, 2, 2)
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (
                    TranscriptSegment(0.0, 2.0, "第一段软件", -0.1, 0.01),
                    TranscriptSegment(2.0, 4.0, "第二段", -0.2, 0.02),
                ),
                normalize_text=lambda text: text.replace("软件", "軟體"),
            )
            store.update_status(session.id, "transcript_ready", duration_seconds=10)

            reopened = SessionStore(root / "data.db")
            saved = reopened.get_transcript_segments(session.id)

            self.assertEqual(
                [segment.text for segment in saved],
                ["第一段軟體", "第二段"],
            )
            rows = reopened.get_transcript_rows(session.id)
            self.assertEqual(rows[0]["raw_text"], "第一段软件")
            self.assertEqual(rows[0]["text"], "第一段軟體")
            self.assertEqual(reopened.list_pending_chunks(session.id), [])
            self.assertEqual(reopened.get_session(session.id).status, "transcript_ready")
            self.assertEqual(len(reopened.list_sessions(incomplete_only=True)), 1)

    def test_failed_chunk_remains_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = SessionStore(root / "data.db")
            session = store.create_session(
                title="失敗測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "exports",
            )
            chunk = AudioChunk(3, root / "chunk.wav", 100, 200, 10, 1, 2)
            chunk.path.write_bytes(b"recoverable")
            store.register_chunk(session.id, chunk)
            store.mark_chunk_failed(session.id, chunk.index, "GPU failure")

            pending = store.list_pending_chunks(session.id)

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].status, "failed")
            self.assertEqual(pending[0].error_message, "GPU failure")

    def test_managed_paths_are_portable_after_userdata_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            original_root = base / "Original" / "UserData"
            original_db = original_root / "Database" / "video_chapter_tool.db"
            store = SessionStore(original_db)
            export_directory = original_root / "Exports" / "portable session"
            session = store.create_session(
                title="Portable 測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=export_directory,
            )
            chunk_path = original_root / "PendingAudio" / "session_1" / "chunk.wav"
            chunk_path.parent.mkdir(parents=True)
            chunk_path.write_bytes(b"recoverable")
            chunk = AudioChunk(0, chunk_path, 0, 100, 10, 1, 2)
            store.register_chunk(session.id, chunk)

            connection = sqlite3.connect(original_db)
            try:
                raw_export = connection.execute(
                    "SELECT export_directory FROM videos WHERE id = ?",
                    (session.id,),
                ).fetchone()[0]
                raw_chunk = connection.execute(
                    "SELECT path FROM pending_chunks WHERE video_id = ?",
                    (session.id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(raw_export, "@data/Exports/portable session")
            self.assertEqual(
                raw_chunk,
                "@data/PendingAudio/session_1/chunk.wav",
            )

            moved_root = base / "Moved" / "UserData"
            shutil.copytree(original_root, moved_root)
            moved_store = SessionStore(
                moved_root / "Database" / "video_chapter_tool.db"
            )

            self.assertEqual(
                moved_store.get_session(session.id).export_directory,
                str((moved_root / "Exports" / "portable session").resolve()),
            )
            self.assertEqual(
                moved_store.list_pending_chunks(session.id)[0].path,
                str((moved_root / "PendingAudio" / "session_1" / "chunk.wav").resolve()),
            )

    def test_schema_five_absolute_managed_paths_are_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "UserData"
            database = root / "Database" / "video_chapter_tool.db"
            store = SessionStore(database)
            session = store.create_session(
                title="舊版路徑",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "Exports" / "legacy",
            )
            chunk_path = root / "PendingAudio" / "session_1" / "legacy.wav"
            chunk_path.parent.mkdir(parents=True)
            chunk_path.write_bytes(b"legacy")
            store.register_chunk(
                session.id,
                AudioChunk(0, chunk_path, 0, 100, 10, 1, 2),
            )

            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE videos SET export_directory = ? WHERE id = ?",
                    (str(root / "Exports" / "legacy"), session.id),
                )
                connection.execute(
                    "UPDATE pending_chunks SET path = ? WHERE video_id = ?",
                    (str(chunk_path), session.id),
                )
                connection.execute("PRAGMA user_version = 5")
                connection.commit()
            finally:
                connection.close()

            SessionStore(database)
            connection = sqlite3.connect(database)
            try:
                raw_export = connection.execute(
                    "SELECT export_directory FROM videos WHERE id = ?",
                    (session.id,),
                ).fetchone()[0]
                raw_chunk = connection.execute(
                    "SELECT path FROM pending_chunks WHERE video_id = ?",
                    (session.id,),
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(raw_export, "@data/Exports/legacy")
            self.assertEqual(raw_chunk, "@data/PendingAudio/session_1/legacy.wav")

    def test_delete_session_cascades_relational_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "UserData"
            store = SessionStore(root / "Database" / "video_chapter_tool.db")
            session = store.create_session(
                title="刪除測試",
                platform=None,
                source=None,
                language="zh",
                model_name="small",
                requested_device="cpu",
                export_directory=root / "Exports",
            )
            chunk_path = root / "PendingAudio" / "session_1" / "chunk.wav"
            chunk_path.parent.mkdir(parents=True)
            chunk_path.write_bytes(b"audio")
            chunk = AudioChunk(0, chunk_path, 0, 100, 10, 1, 2)
            store.register_chunk(session.id, chunk)
            store.commit_chunk_transcript(
                session.id,
                chunk,
                (TranscriptSegment(0, 1, "測試", -0.1, 0.01),),
            )

            store.delete_session(session.id)

            with self.assertRaises(DatabaseError):
                store.get_session(session.id)
            connection = sqlite3.connect(store.path)
            try:
                transcript_count = connection.execute(
                    "SELECT COUNT(*) FROM transcripts WHERE video_id = ?",
                    (session.id,),
                ).fetchone()[0]
                chunk_count = connection.execute(
                    "SELECT COUNT(*) FROM pending_chunks WHERE video_id = ?",
                    (session.id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(transcript_count, 0)
            self.assertEqual(chunk_count, 0)


if __name__ == "__main__":
    unittest.main()
