from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.database import SessionStore
from app.diarization import LabeledTranscriptSegment, SpeakerTurn, label_transcript_segments


class ExportError(RuntimeError):
    """Raised when transcript export cannot complete."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    directory: Path
    txt_path: Path
    json_path: Path
    srt_path: Path

    def to_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


def _safe_directory_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return (value[:80].rstrip(" .") or "未命名影片")


def _timestamp(seconds: float, *, srt: bool) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


def _write_text_atomic(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _speaker_prefix(segment: LabeledTranscriptSegment) -> str:
    name = segment.speaker_name or segment.speaker_id
    return f"{name}：" if name else ""


def _txt_content(segments: Iterable[LabeledTranscriptSegment]) -> str:
    blocks = [
        f"[{_timestamp(segment.start, srt=False)}]\n"
        f"{_speaker_prefix(segment)}{segment.text}"
        for segment in segments
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _srt_content(segments: Iterable[LabeledTranscriptSegment]) -> str:
    blocks = [
        f"{index}\n"
        f"{_timestamp(segment.start, srt=True)} --> "
        f"{_timestamp(segment.end, srt=True)}\n"
        f"{_speaker_prefix(segment)}{segment.text}"
        for index, segment in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _choose_directory(root: Path, title: str, created_at: str) -> Path:
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is not None:
            created = created.astimezone()
        timestamp = created.strftime("%Y%m%d_%H%M%S")
    except ValueError:
        timestamp = "unknown_time"
    base_name = f"{_safe_directory_name(title)}_{timestamp}"
    candidate = root / base_name
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base_name}_{suffix}"
        suffix += 1
    return candidate


def export_session(
    store: SessionStore,
    session_id: int,
    *,
    output_root: Path | None = None,
) -> ExportResult:
    session = store.get_session(session_id)
    segments = store.get_transcript_segments(session_id)
    transcript_rows = store.get_transcript_rows(session_id)
    speaker_turn_rows = store.get_speaker_turns(session_id)
    speaker_label_rows = store.get_speaker_labels(session_id)
    speaker_turns = tuple(
        SpeakerTurn(
            start=row.start_time,
            end=row.end_time,
            speaker_id=row.speaker_id,
        )
        for row in speaker_turn_rows
    )
    speaker_labels = {
        row.speaker_id: row.display_name for row in speaker_label_rows
    }
    labeled_segments = label_transcript_segments(
        segments,
        speaker_turns,
        labels=speaker_labels,
    )
    for row, labeled in zip(transcript_rows, labeled_segments, strict=True):
        row["speaker_id"] = labeled.speaker_id
        row["speaker_name"] = labeled.speaker_name
    root = (
        output_root.expanduser().resolve()
        if output_root is not None
        else Path(session.export_directory).expanduser().resolve()
    )
    try:
        root.mkdir(parents=True, exist_ok=True)
        if session.exported_path:
            directory = Path(session.exported_path).expanduser().resolve()
        else:
            directory = _choose_directory(root, session.title, session.created_at)
        directory.mkdir(parents=True, exist_ok=True)

        txt_path = directory / "transcript.txt"
        json_path = directory / "transcript.json"
        srt_path = directory / "subtitles.srt"
        session_payload = session.to_dict()
        session_payload["exported_path"] = str(directory)
        payload = {
            "schema_version": 3,
            "session": session_payload,
            "segments": transcript_rows,
            "speaker_turns": [row.to_dict() for row in speaker_turn_rows],
            "speaker_labels": [row.to_dict() for row in speaker_label_rows],
        }
        _write_text_atomic(txt_path, _txt_content(labeled_segments))
        _write_text_atomic(
            json_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        _write_text_atomic(srt_path, _srt_content(labeled_segments))
        expected_files = (txt_path, json_path, srt_path)
        if not all(path.is_file() for path in expected_files):
            raise ExportError("逐字稿匯出後檔案驗證失敗。")
        store.set_exported_path(session_id, directory)
        return ExportResult(
            directory=directory,
            txt_path=txt_path,
            json_path=json_path,
            srt_path=srt_path,
        )
    except ExportError:
        raise
    except (OSError, ValueError) as exc:
        raise ExportError(f"逐字稿匯出失敗：{exc}") from exc
