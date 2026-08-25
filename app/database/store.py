from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping

from app.audio.chunking import AudioChunk

if TYPE_CHECKING:
    from app.transcription.transcriber import TranscriptSegment
    from app.diarization.models import SpeakerTurn


SCHEMA_VERSION = 6
DATA_PATH_PREFIX = "@data/"
SESSION_STATUSES = {
    "created",
    "capturing",
    "draining_queue",
    "transcript_ready",
    "ai_analyzing",
    "diarization_pending",
    "diarizing",
    "completed",
    "failed",
    "cancelled",
}
INCOMPLETE_SESSION_STATUSES = {
    "created",
    "capturing",
    "draining_queue",
    "transcript_ready",
    "ai_analyzing",
    "diarization_pending",
    "diarizing",
    "failed",
}
PENDING_CHUNK_STATUSES = {"pending", "processing", "completed", "failed"}


class DatabaseError(RuntimeError):
    """Raised when durable application data cannot be read or written."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: int
    title: str
    platform: str | None
    source: str | None
    created_at: str
    updated_at: str
    duration_seconds: float
    status: str
    language: str | None
    model_name: str
    requested_device: str
    resolved_device: str | None
    compute_type: str | None
    chunk_seconds: float
    overlap_seconds: float
    export_directory: str
    exported_path: str | None
    stability_report_path: str | None
    overall_summary: str | None
    ai_provider: str | None
    ai_model: str | None
    ai_completed_at: str | None
    diarization_provider: str | None
    diarization_model: str | None
    diarization_status: str | None
    diarization_error: str | None
    requested_speaker_count: int | None
    diarization_completed_at: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PendingChunkRecord:
    id: int
    video_id: int
    chunk_index: int
    path: str
    start_time: float
    end_time: float
    status: str
    error_message: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChapterRecord:
    id: int
    video_id: int
    start_time: float
    end_time: float
    title: str
    summary: str
    sort_order: int
    created_at: str
    key_points: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["key_points"] = list(self.key_points)
        return result


@dataclass(frozen=True, slots=True)
class SpeakerTurnRecord:
    id: int
    video_id: int
    start_time: float
    end_time: float
    speaker_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SpeakerLabelRecord:
    video_id: int
    speaker_id: str
    display_name: str
    sort_order: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path.expanduser().resolve()
        self.data_root = self.path.parent.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _encode_path(self, path: str | Path) -> str:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.data_root / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.data_root)
        except ValueError:
            return str(resolved)
        return DATA_PATH_PREFIX + relative.as_posix()

    def _decode_path(self, value: str) -> str:
        if value.startswith(DATA_PATH_PREFIX):
            relative = value[len(DATA_PATH_PREFIX) :]
            return str((self.data_root / Path(relative)).resolve())
        return str(Path(value).expanduser().resolve())

    def _session_record(self, row: sqlite3.Row) -> SessionRecord:
        values = dict(row)
        for field in (
            "export_directory",
            "exported_path",
            "stability_report_path",
        ):
            if values.get(field):
                values[field] = self._decode_path(str(values[field]))
        return SessionRecord(**values)

    def _chunk_record(self, row: sqlite3.Row) -> PendingChunkRecord:
        values = dict(row)
        values["path"] = self._decode_path(str(values["path"]))
        return PendingChunkRecord(**values)

    def _migrate_managed_paths(self, connection: sqlite3.Connection) -> None:
        for table, identifier, columns in (
            (
                "videos",
                "id",
                ("export_directory", "exported_path", "stability_report_path"),
            ),
            ("pending_chunks", "id", ("path",)),
        ):
            selected = ", ".join((identifier, *columns))
            rows = connection.execute(f"SELECT {selected} FROM {table}").fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in columns:
                    value = row[column]
                    if not value or str(value).startswith(DATA_PATH_PREFIX):
                        continue
                    encoded = self._encode_path(str(value))
                    if encoded.startswith(DATA_PATH_PREFIX):
                        updates[column] = encoded
                if updates:
                    assignments = ", ".join(f"{column} = ?" for column in updates)
                    connection.execute(
                        f"UPDATE {table} SET {assignments} WHERE {identifier} = ?",
                        (*updates.values(), row[identifier]),
                    )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            with self._connect() as connection:
                previous_schema_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS videos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        platform TEXT,
                        source TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        duration_seconds REAL NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        language TEXT,
                        model_name TEXT NOT NULL,
                        requested_device TEXT NOT NULL,
                        resolved_device TEXT,
                        compute_type TEXT,
                        chunk_seconds REAL NOT NULL DEFAULT 25,
                        overlap_seconds REAL NOT NULL DEFAULT 2,
                        export_directory TEXT NOT NULL,
                        exported_path TEXT,
                        stability_report_path TEXT,
                        overall_summary TEXT,
                        ai_provider TEXT,
                        ai_model TEXT,
                        ai_completed_at TEXT,
                        diarization_provider TEXT,
                        diarization_model TEXT,
                        diarization_status TEXT,
                        diarization_error TEXT,
                        requested_speaker_count INTEGER,
                        diarization_completed_at TEXT,
                        error_message TEXT
                    );

                    CREATE TABLE IF NOT EXISTS transcripts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        chunk_index INTEGER NOT NULL,
                        segment_index INTEGER NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        raw_text TEXT NOT NULL,
                        text TEXT NOT NULL,
                        average_log_probability REAL,
                        no_speech_probability REAL,
                        speaker_id TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(video_id, chunk_index, segment_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_transcripts_video_time
                    ON transcripts(video_id, start_time, end_time, id);

                    CREATE TABLE IF NOT EXISTS pending_chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        chunk_index INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(video_id, chunk_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_pending_chunks_video_status
                    ON pending_chunks(video_id, status, chunk_index);

                    CREATE TABLE IF NOT EXISTS chapters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        sort_order INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS chapter_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                        content TEXT NOT NULL,
                        sort_order INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS ai_analysis_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        batch_index INTEGER NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        provider_name TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        response_json TEXT,
                        error_message TEXT,
                        updated_at TEXT NOT NULL,
                        UNIQUE(video_id, batch_index, provider_name, model_name)
                    );

                    CREATE INDEX IF NOT EXISTS idx_ai_batches_video_status
                    ON ai_analysis_batches(video_id, status, batch_index);

                    CREATE TABLE IF NOT EXISTS transcript_words (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        word_index INTEGER NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        raw_text TEXT NOT NULL,
                        text TEXT NOT NULL,
                        probability REAL,
                        UNIQUE(transcript_id, word_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_transcript_words_video_time
                    ON transcript_words(video_id, start_time, end_time, id);

                    CREATE TABLE IF NOT EXISTS speaker_turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        speaker_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_speaker_turns_video_time
                    ON speaker_turns(video_id, start_time, end_time, id);

                    CREATE TABLE IF NOT EXISTS speaker_labels (
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        speaker_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        sort_order INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(video_id, speaker_id)
                    );
                    """
                )
                video_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(videos)")
                }
                if "chunk_seconds" not in video_columns:
                    connection.execute(
                        "ALTER TABLE videos ADD COLUMN "
                        "chunk_seconds REAL NOT NULL DEFAULT 25"
                    )
                if "overlap_seconds" not in video_columns:
                    connection.execute(
                        "ALTER TABLE videos ADD COLUMN "
                        "overlap_seconds REAL NOT NULL DEFAULT 2"
                    )
                if "stability_report_path" not in video_columns:
                    connection.execute(
                        "ALTER TABLE videos ADD COLUMN stability_report_path TEXT"
                    )
                for column_name in (
                    "overall_summary",
                    "ai_provider",
                    "ai_model",
                    "ai_completed_at",
                    "diarization_provider",
                    "diarization_model",
                    "diarization_status",
                    "diarization_error",
                    "diarization_completed_at",
                ):
                    if column_name not in video_columns:
                        connection.execute(
                            f"ALTER TABLE videos ADD COLUMN {column_name} TEXT"
                        )
                if "requested_speaker_count" not in video_columns:
                    connection.execute(
                        "ALTER TABLE videos ADD COLUMN requested_speaker_count INTEGER"
                    )
                if previous_schema_version < 6:
                    self._migrate_managed_paths(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"無法初始化 SQLite：{exc}") from exc

    def create_session(
        self,
        *,
        title: str,
        platform: str | None,
        source: str | None,
        language: str | None,
        model_name: str,
        requested_device: str,
        export_directory: Path,
        chunk_seconds: float = 25.0,
        overlap_seconds: float = 2.0,
    ) -> SessionRecord:
        title = title.strip()
        if not title:
            raise ValueError("工作階段標題不可為空。")
        timestamp = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO videos (
                        title, platform, source, created_at, updated_at,
                        duration_seconds, status, language, model_name,
                        requested_device, chunk_seconds, overlap_seconds,
                        export_directory
                    ) VALUES (?, ?, ?, ?, ?, 0, 'created', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        platform,
                        source,
                        timestamp,
                        timestamp,
                        language,
                        model_name,
                        requested_device,
                        float(chunk_seconds),
                        float(overlap_seconds),
                        self._encode_path(export_directory),
                    ),
                )
                session_id = int(cursor.lastrowid)
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"無法建立工作階段：{exc}") from exc
        return self.get_session(session_id)

    def get_session(self, session_id: int) -> SessionRecord:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM videos WHERE id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取工作階段：{exc}") from exc
        if row is None:
            raise DatabaseError(f"找不到工作階段：{session_id}")
        return self._session_record(row)

    def list_sessions(self, *, incomplete_only: bool = False) -> list[SessionRecord]:
        parameters: tuple[Any, ...] = ()
        where = ""
        if incomplete_only:
            placeholders = ",".join("?" for _ in INCOMPLETE_SESSION_STATUSES)
            where = f"WHERE status IN ({placeholders})"
            parameters = tuple(sorted(INCOMPLETE_SESSION_STATUSES))
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT * FROM videos {where} ORDER BY id DESC",
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法列出工作階段：{exc}") from exc
        return [self._session_record(row) for row in rows]

    def delete_session(self, session_id: int) -> None:
        """Delete one session and its relational data through foreign-key cascades."""

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM videos WHERE id = ?",
                    (session_id,),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(f"找不到工作階段：{session_id}")
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法刪除工作階段：{exc}") from exc

    def update_status(
        self,
        session_id: int,
        status: str,
        *,
        duration_seconds: float | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in SESSION_STATUSES:
            raise ValueError(f"不支援的工作階段狀態：{status}")
        timestamp = _now()
        fields = ["status = ?", "updated_at = ?", "error_message = ?"]
        values: list[Any] = [status, timestamp, error_message]
        if duration_seconds is not None:
            fields.append("duration_seconds = ?")
            values.append(max(0.0, float(duration_seconds)))
        values.append(session_id)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"UPDATE videos SET {', '.join(fields)} WHERE id = ?",
                    tuple(values),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(f"找不到工作階段：{session_id}")
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法更新工作階段狀態：{exc}") from exc

    def update_runtime(
        self,
        session_id: int,
        *,
        resolved_device: str,
        compute_type: str,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE videos
                    SET resolved_device = ?, compute_type = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (resolved_device, compute_type, _now(), session_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法保存運算裝置資訊：{exc}") from exc

    def register_chunk(self, session_id: int, chunk: AudioChunk) -> None:
        timestamp = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO pending_chunks (
                        video_id, chunk_index, path, start_time, end_time,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(video_id, chunk_index) DO UPDATE SET
                        path = excluded.path,
                        start_time = excluded.start_time,
                        end_time = excluded.end_time,
                        status = 'pending',
                        error_message = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        chunk.index,
                        self._encode_path(chunk.path),
                        chunk.start_seconds,
                        chunk.end_seconds,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法登記待處理音訊 Chunk：{exc}") from exc

    def commit_chunk_transcript(
        self,
        session_id: int,
        chunk: AudioChunk,
        segments: Iterable[TranscriptSegment],
        *,
        normalize_text: Callable[[str], str] | None = None,
    ) -> None:
        segment_values = tuple(segments)
        timestamp = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE pending_chunks
                    SET status = 'processing', error_message = NULL, updated_at = ?
                    WHERE video_id = ? AND chunk_index = ?
                    """,
                    (timestamp, session_id, chunk.index),
                )
                connection.execute(
                    "DELETE FROM transcripts WHERE video_id = ? AND chunk_index = ?",
                    (session_id, chunk.index),
                )
                for segment_index, segment in enumerate(segment_values):
                    cursor = connection.execute(
                        """
                        INSERT INTO transcripts (
                            video_id, chunk_index, segment_index, start_time,
                            end_time, raw_text, text, average_log_probability,
                            no_speech_probability, speaker_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            session_id,
                            chunk.index,
                            segment_index,
                            segment.start,
                            segment.end,
                            segment.text,
                            (
                                normalize_text(segment.text)
                                if normalize_text is not None
                                else segment.text
                            ),
                            segment.average_log_probability,
                            segment.no_speech_probability,
                            timestamp,
                        ),
                    )
                    transcript_id = int(cursor.lastrowid)
                    connection.executemany(
                        """
                        INSERT INTO transcript_words (
                            transcript_id, video_id, word_index, start_time,
                            end_time, raw_text, text, probability
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                transcript_id,
                                session_id,
                                word_index,
                                word.start,
                                word.end,
                                word.text,
                                (
                                    normalize_text(word.text)
                                    if normalize_text is not None
                                    else word.text
                                ),
                                word.probability,
                            )
                            for word_index, word in enumerate(segment.words)
                        ),
                    )
                cursor = connection.execute(
                    """
                    UPDATE pending_chunks
                    SET status = 'completed', updated_at = ?
                    WHERE video_id = ? AND chunk_index = ?
                    """,
                    (timestamp, session_id, chunk.index),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(
                        f"工作階段 {session_id} 找不到 Chunk {chunk.index}。"
                    )
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法交易保存逐字稿 Chunk：{exc}") from exc

    def mark_chunk_failed(
        self,
        session_id: int,
        chunk_index: int,
        error_message: str,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE pending_chunks
                    SET status = 'failed', error_message = ?, updated_at = ?
                    WHERE video_id = ? AND chunk_index = ?
                    """,
                    (error_message[:500], _now(), session_id, chunk_index),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法記錄 Chunk 失敗狀態：{exc}") from exc

    def list_pending_chunks(self, session_id: int) -> list[PendingChunkRecord]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM pending_chunks
                    WHERE video_id = ? AND status != 'completed'
                    ORDER BY chunk_index
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取待處理 Chunk：{exc}") from exc
        return [self._chunk_record(row) for row in rows]

    def list_session_chunks(self, session_id: int) -> list[PendingChunkRecord]:
        """Return every registered Chunk, including retained completed audio."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM pending_chunks
                    WHERE video_id = ?
                    ORDER BY chunk_index
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取工作階段音訊 Chunk：{exc}") from exc
        return [self._chunk_record(row) for row in rows]

    def get_transcript_segments(self, session_id: int) -> tuple[TranscriptSegment, ...]:
        from app.transcription.transcriber import TranscriptSegment, TranscriptWord

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, start_time, end_time, text,
                           average_log_probability, no_speech_probability
                    FROM transcripts
                    WHERE video_id = ?
                    ORDER BY start_time, end_time, id
                    """,
                    (session_id,),
                ).fetchall()
                word_rows = connection.execute(
                    """
                    SELECT transcript_id, start_time, end_time, text, probability
                    FROM transcript_words
                    WHERE video_id = ?
                    ORDER BY transcript_id, word_index
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取逐字稿：{exc}") from exc
        words_by_transcript: dict[int, list[TranscriptWord]] = {}
        for row in word_rows:
            words_by_transcript.setdefault(int(row["transcript_id"]), []).append(
                TranscriptWord(
                    start=float(row["start_time"]),
                    end=float(row["end_time"]),
                    text=str(row["text"]),
                    probability=(
                        float(row["probability"])
                        if row["probability"] is not None
                        else None
                    ),
                )
            )
        return tuple(
            TranscriptSegment(
                start=float(row["start_time"]),
                end=float(row["end_time"]),
                text=str(row["text"]),
                average_log_probability=float(
                    row["average_log_probability"] or 0.0
                ),
                no_speech_probability=float(row["no_speech_probability"] or 0.0),
                words=tuple(words_by_transcript.get(int(row["id"]), ())),
            )
            for row in rows
        )

    def get_transcript_rows(self, session_id: int) -> list[dict[str, Any]]:
        """Return export-ready rows including both raw and normalized text."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, start_time, end_time, raw_text, text,
                           average_log_probability, no_speech_probability,
                           speaker_id
                    FROM transcripts
                    WHERE video_id = ?
                    ORDER BY start_time, end_time, id
                    """,
                    (session_id,),
                ).fetchall()
                word_rows = connection.execute(
                    """
                    SELECT transcript_id, start_time, end_time, raw_text, text,
                           probability
                    FROM transcript_words
                    WHERE video_id = ?
                    ORDER BY transcript_id, word_index
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取逐字稿匯出資料：{exc}") from exc
        words_by_transcript: dict[int, list[dict[str, Any]]] = {}
        for word_row in word_rows:
            word = dict(word_row)
            transcript_id = int(word.pop("transcript_id"))
            words_by_transcript.setdefault(transcript_id, []).append(word)
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            transcript_id = int(item.pop("id"))
            item["words"] = words_by_transcript.get(transcript_id, [])
            result.append(item)
        return result

    def set_diarization_status(
        self,
        session_id: int,
        status: str | None,
        *,
        provider_name: str | None = None,
        model_name: str | None = None,
        requested_speaker_count: int | None = None,
        error_message: str | None = None,
    ) -> None:
        allowed = {None, "pending", "analyzing", "completed", "failed", "skipped"}
        if status not in allowed:
            raise ValueError(f"不支援的說話者分析狀態：{status}")
        if requested_speaker_count is not None and not 1 <= requested_speaker_count <= 6:
            raise ValueError("指定說話者人數必須介於 1～6。")
        completed_at = _now() if status == "completed" else None
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE videos
                    SET diarization_provider = COALESCE(?, diarization_provider),
                        diarization_model = COALESCE(?, diarization_model),
                        diarization_status = ?, diarization_error = ?,
                        requested_speaker_count = ?,
                        diarization_completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        provider_name,
                        model_name,
                        status,
                        error_message[:500] if error_message else None,
                        requested_speaker_count,
                        completed_at,
                        _now(),
                        session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(f"找不到工作階段：{session_id}")
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法保存說話者分析狀態：{exc}") from exc

    def replace_speaker_turns(
        self,
        session_id: int,
        turns: Iterable[SpeakerTurn],
        *,
        provider_name: str,
        model_name: str,
        requested_speaker_count: int | None,
    ) -> None:
        from app.diarization import validate_speaker_turns

        session = self.get_session(session_id)
        turn_values = validate_speaker_turns(
            turns,
            duration_seconds=session.duration_seconds or None,
        )
        if not turn_values:
            raise ValueError("說話者分析結果不可為空。")
        speaker_order: list[str] = []
        for turn in turn_values:
            if turn.speaker_id not in speaker_order:
                speaker_order.append(turn.speaker_id)
        timestamp = _now()
        try:
            with self._connect() as connection:
                existing_names = {
                    str(row["speaker_id"]): str(row["display_name"])
                    for row in connection.execute(
                        """
                        SELECT speaker_id, display_name FROM speaker_labels
                        WHERE video_id = ?
                        """,
                        (session_id,),
                    ).fetchall()
                }
                connection.execute(
                    "DELETE FROM speaker_turns WHERE video_id = ?",
                    (session_id,),
                )
                connection.executemany(
                    """
                    INSERT INTO speaker_turns (
                        video_id, start_time, end_time, speaker_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            session_id,
                            turn.start,
                            turn.end,
                            turn.speaker_id,
                            timestamp,
                        )
                        for turn in turn_values
                    ),
                )
                connection.execute(
                    "DELETE FROM speaker_labels WHERE video_id = ?",
                    (session_id,),
                )
                connection.executemany(
                    """
                    INSERT INTO speaker_labels (
                        video_id, speaker_id, display_name, sort_order, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            session_id,
                            speaker_id,
                            existing_names.get(
                                speaker_id,
                                "說話者 " + speaker_id.removeprefix("speaker_"),
                            ),
                            sort_order,
                            timestamp,
                        )
                        for sort_order, speaker_id in enumerate(speaker_order)
                    ),
                )
                connection.execute(
                    """
                    UPDATE videos
                    SET diarization_provider = ?, diarization_model = ?,
                        diarization_status = 'completed', diarization_error = NULL,
                        requested_speaker_count = ?, diarization_completed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        provider_name,
                        model_name,
                        requested_speaker_count,
                        timestamp,
                        timestamp,
                        session_id,
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法交易保存說話者分析結果：{exc}") from exc

    def get_speaker_turns(self, session_id: int) -> tuple[SpeakerTurnRecord, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM speaker_turns
                    WHERE video_id = ?
                    ORDER BY start_time, end_time, id
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取說話者時間區段：{exc}") from exc
        return tuple(SpeakerTurnRecord(**dict(row)) for row in rows)

    def get_speaker_labels(self, session_id: int) -> tuple[SpeakerLabelRecord, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM speaker_labels
                    WHERE video_id = ?
                    ORDER BY sort_order, speaker_id
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取說話者名稱：{exc}") from exc
        return tuple(SpeakerLabelRecord(**dict(row)) for row in rows)

    def rename_speaker(
        self,
        session_id: int,
        speaker_id: str,
        display_name: str,
    ) -> None:
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("說話者名稱不可為空。")
        if len(display_name) > 80:
            raise ValueError("說話者名稱不可超過 80 個字元。")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE speaker_labels
                    SET display_name = ?, updated_at = ?
                    WHERE video_id = ? AND speaker_id = ?
                    """,
                    (display_name, _now(), session_id, speaker_id),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(
                        f"工作階段 {session_id} 找不到說話者 {speaker_id}。"
                    )
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法重新命名說話者：{exc}") from exc

    def get_recorded_timeline_end(self, session_id: int) -> float:
        """Return the furthest durable transcript or Chunk timestamp."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT MAX(value) AS maximum_end
                    FROM (
                        SELECT MAX(end_time) AS value
                        FROM transcripts WHERE video_id = ?
                        UNION ALL
                        SELECT MAX(end_time) AS value
                        FROM pending_chunks WHERE video_id = ?
                    )
                    """,
                    (session_id, session_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取已保存時間範圍：{exc}") from exc
        if row is None or row["maximum_end"] is None:
            return 0.0
        return max(0.0, float(row["maximum_end"]))

    def get_cached_ai_batch(
        self,
        session_id: int,
        *,
        batch_index: int,
        provider_name: str,
        model_name: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT response_json
                    FROM ai_analysis_batches
                    WHERE video_id = ? AND batch_index = ?
                      AND provider_name = ? AND model_name = ?
                      AND request_hash = ? AND status = 'completed'
                    """,
                    (
                        session_id,
                        batch_index,
                        provider_name,
                        model_name,
                        request_hash,
                    ),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取 AI 分批快取：{exc}") from exc
        if row is None or row["response_json"] is None:
            return None
        try:
            value = json.loads(str(row["response_json"]))
        except (TypeError, ValueError) as exc:
            raise DatabaseError("AI 分批快取不是有效的 JSON。") from exc
        if not isinstance(value, dict):
            raise DatabaseError("AI 分批快取必須是 JSON 物件。")
        return value

    def save_ai_batch_success(
        self,
        session_id: int,
        *,
        batch_index: int,
        start_time: float,
        end_time: float,
        provider_name: str,
        model_name: str,
        request_hash: str,
        attempts: int,
        response: Mapping[str, Any],
    ) -> None:
        self._upsert_ai_batch(
            session_id,
            batch_index=batch_index,
            start_time=start_time,
            end_time=end_time,
            provider_name=provider_name,
            model_name=model_name,
            request_hash=request_hash,
            status="completed",
            attempts=attempts,
            response_json=json.dumps(response, ensure_ascii=False),
            error_message=None,
        )

    def save_ai_batch_failure(
        self,
        session_id: int,
        *,
        batch_index: int,
        start_time: float,
        end_time: float,
        provider_name: str,
        model_name: str,
        request_hash: str,
        attempts: int,
        error_message: str,
    ) -> None:
        self._upsert_ai_batch(
            session_id,
            batch_index=batch_index,
            start_time=start_time,
            end_time=end_time,
            provider_name=provider_name,
            model_name=model_name,
            request_hash=request_hash,
            status="failed",
            attempts=attempts,
            response_json=None,
            error_message=error_message[:1000],
        )

    def _upsert_ai_batch(
        self,
        session_id: int,
        *,
        batch_index: int,
        start_time: float,
        end_time: float,
        provider_name: str,
        model_name: str,
        request_hash: str,
        status: str,
        attempts: int,
        response_json: str | None,
        error_message: str | None,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO ai_analysis_batches (
                        video_id, batch_index, start_time, end_time,
                        provider_name, model_name, request_hash, status,
                        attempts, response_json, error_message, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id, batch_index, provider_name, model_name)
                    DO UPDATE SET
                        start_time = excluded.start_time,
                        end_time = excluded.end_time,
                        request_hash = excluded.request_hash,
                        status = excluded.status,
                        attempts = excluded.attempts,
                        response_json = excluded.response_json,
                        error_message = excluded.error_message,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        batch_index,
                        start_time,
                        end_time,
                        provider_name,
                        model_name,
                        request_hash,
                        status,
                        max(0, attempts),
                        response_json,
                        error_message,
                        _now(),
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法保存 AI 分批結果：{exc}") from exc

    def replace_ai_result(
        self,
        session_id: int,
        *,
        overall_summary: str,
        provider_name: str,
        model_name: str,
        chapters: Iterable[Mapping[str, Any]],
    ) -> None:
        chapter_values = tuple(chapters)
        timestamp = _now()
        try:
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM videos WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise DatabaseError(f"找不到工作階段：{session_id}")
                connection.execute(
                    "DELETE FROM chapters WHERE video_id = ?",
                    (session_id,),
                )
                for sort_order, chapter in enumerate(chapter_values):
                    cursor = connection.execute(
                        """
                        INSERT INTO chapters (
                            video_id, start_time, end_time, title,
                            summary, sort_order, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            float(chapter["start"]),
                            float(chapter["end"]),
                            str(chapter["title"]),
                            str(chapter["summary"]),
                            sort_order,
                            timestamp,
                        ),
                    )
                    chapter_id = int(cursor.lastrowid)
                    points = chapter.get("key_points", ())
                    connection.executemany(
                        """
                        INSERT INTO chapter_points (
                            chapter_id, content, sort_order
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            (chapter_id, str(point), point_order)
                            for point_order, point in enumerate(points)
                        ),
                    )
                connection.execute(
                    """
                    UPDATE videos
                    SET overall_summary = ?, ai_provider = ?, ai_model = ?,
                        ai_completed_at = ?, status = 'completed',
                        error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        overall_summary,
                        provider_name,
                        model_name,
                        timestamp,
                        timestamp,
                        session_id,
                    ),
                )
        except DatabaseError:
            raise
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            raise DatabaseError(f"無法保存 AI 章節結果：{exc}") from exc

    def get_chapters(self, session_id: int) -> tuple[ChapterRecord, ...]:
        try:
            with self._connect() as connection:
                chapter_rows = connection.execute(
                    """
                    SELECT * FROM chapters
                    WHERE video_id = ?
                    ORDER BY sort_order, start_time, id
                    """,
                    (session_id,),
                ).fetchall()
                point_rows = connection.execute(
                    """
                    SELECT chapter_id, content
                    FROM chapter_points
                    WHERE chapter_id IN (
                        SELECT id FROM chapters WHERE video_id = ?
                    )
                    ORDER BY chapter_id, sort_order, id
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法讀取 AI 章節：{exc}") from exc
        points_by_chapter: dict[int, list[str]] = {}
        for row in point_rows:
            points_by_chapter.setdefault(int(row["chapter_id"]), []).append(
                str(row["content"])
            )
        return tuple(
            ChapterRecord(
                **dict(row),
                key_points=tuple(points_by_chapter.get(int(row["id"]), ())),
            )
            for row in chapter_rows
        )

    def set_exported_path(self, session_id: int, exported_path: Path) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE videos
                    SET exported_path = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._encode_path(exported_path), _now(), session_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法保存匯出位置：{exc}") from exc

    def set_stability_report_path(
        self,
        session_id: int,
        report_path: Path,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE videos
                    SET stability_report_path = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._encode_path(report_path), _now(), session_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"無法保存穩定性報告位置：{exc}") from exc
