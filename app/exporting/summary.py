from __future__ import annotations

from pathlib import Path

from app.database import SessionStore
from app.exporting.transcript import _timestamp, _write_text_atomic, export_session


class SummaryExportError(RuntimeError):
    """Raised when an AI chapter summary cannot be exported."""


def export_summary(store: SessionStore, session_id: int) -> Path:
    session = store.get_session(session_id)
    chapters = store.get_chapters(session_id)
    if not session.overall_summary or not chapters:
        raise SummaryExportError("工作階段尚未完成 AI 摘要與章節分析。")

    try:
        if session.exported_path:
            directory = Path(session.exported_path).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
        else:
            directory = export_session(store, session_id).directory
        lines = [
            f"# {session.title}",
            "",
            "## 完整摘要",
            "",
            session.overall_summary,
            "",
            "## 章節導覽",
            "",
        ]
        for chapter in chapters:
            start = _timestamp(chapter.start_time, srt=False)
            end = _timestamp(chapter.end_time, srt=False)
            lines.extend(
                [
                    f"### {start} {chapter.title}",
                    "",
                    f"時間：{start}–{end}",
                    "",
                    chapter.summary,
                    "",
                ]
            )
            lines.extend(f"- {point}" for point in chapter.key_points)
            lines.append("")
        output_path = directory / "summary.md"
        _write_text_atomic(output_path, "\n".join(lines).rstrip() + "\n")
        return output_path
    except OSError as exc:
        raise SummaryExportError(f"AI 摘要匯出失敗：{exc}") from exc
