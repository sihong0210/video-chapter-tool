from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APP_DIRECTORY_NAME = "VideoChapterTool"
PORTABLE_DATA_DIRECTORY_NAME = "UserData"
DATA_DIR_ENVIRONMENT_VARIABLE = "VIDEO_CHAPTER_TOOL_DATA_DIR"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All application-managed paths rooted under one writable directory."""

    root: Path
    config: Path
    database: Path
    logs: Path
    models: Path
    cache: Path
    pending_audio: Path
    exports: Path
    diagnostics: Path
    stability_reports: Path

    @classmethod
    def from_root(cls, root: str | os.PathLike[str]) -> "AppPaths":
        resolved_root = Path(root).expanduser().resolve()
        return cls(
            root=resolved_root,
            config=resolved_root / "Config",
            database=resolved_root / "Database",
            logs=resolved_root / "Logs",
            models=resolved_root / "Models",
            cache=resolved_root / "Cache",
            pending_audio=resolved_root / "PendingAudio",
            exports=resolved_root / "Exports",
            diagnostics=resolved_root / "Diagnostics",
            stability_reports=resolved_root / "Diagnostics" / "Stability",
        )

    @classmethod
    def default(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        frozen: bool | None = None,
        executable: str | os.PathLike[str] | None = None,
    ) -> "AppPaths":
        environment = os.environ if environment is None else environment

        override = environment.get(DATA_DIR_ENVIRONMENT_VARIABLE, "").strip()
        if override:
            return cls.from_root(override)

        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        if is_frozen:
            executable_path = Path(executable or sys.executable).expanduser().resolve()
            return cls.from_root(
                executable_path.parent / PORTABLE_DATA_DIRECTORY_NAME
            )

        local_app_data = environment.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return cls.from_root(Path(local_app_data) / APP_DIRECTORY_NAME)

        xdg_data_home = environment.get("XDG_DATA_HOME", "").strip()
        if xdg_data_home:
            return cls.from_root(Path(xdg_data_home) / APP_DIRECTORY_NAME)

        return cls.from_root(Path.home() / ".local" / "share" / APP_DIRECTORY_NAME)

    def resolve_configured_path(self, value: str | os.PathLike[str]) -> Path:
        """Resolve portable relative settings against the managed data root."""

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def serialize_configured_path(self, value: str | os.PathLike[str]) -> str:
        """Store managed paths relatively so moving the portable folder is safe."""

        resolved = self.resolve_configured_path(value)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            return str(resolved)
        return str(relative) or "."

    @property
    def settings_file(self) -> Path:
        return self.config / "settings.json"

    @property
    def database_file(self) -> Path:
        return self.database / "video_chapter_tool.db"

    @property
    def application_log_file(self) -> Path:
        return self.logs / "application.log"

    def managed_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.config,
            self.database,
            self.logs,
            self.models,
            self.cache,
            self.pending_audio,
            self.exports,
            self.diagnostics,
            self.stability_reports,
        )

    def ensure(self) -> None:
        for directory in self.managed_directories():
            directory.mkdir(parents=True, exist_ok=True)
