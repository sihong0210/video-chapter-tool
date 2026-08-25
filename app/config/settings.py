from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config.paths import AppPaths


SETTINGS_SCHEMA_VERSION = 5
SUPPORTED_COMPUTE_DEVICES = {"auto", "cpu", "cuda", "amd_experimental"}
SUPPORTED_DIARIZATION_DEVICES = {"auto", "cpu", "cuda"}
SUPPORTED_SPEAKER_MODES = {"off", "auto", "fixed"}
SUPPORTED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class SettingsError(RuntimeError):
    """Raised when settings cannot be safely loaded or validated."""


@dataclass(slots=True)
class AppSettings:
    schema_version: int
    language: str
    whisper_model: str
    compute_device: str
    model_directory: str
    export_directory: str
    log_level: str
    traditional_chinese_output: bool
    speaker_mode: str
    speaker_count: int
    diarization_device: str
    keep_display_on_during_recording: bool

    @classmethod
    def defaults(cls, paths: AppPaths) -> "AppSettings":
        return cls(
            schema_version=SETTINGS_SCHEMA_VERSION,
            language="zh",
            whisper_model="small",
            compute_device="auto",
            model_directory=str(paths.models),
            export_directory=str(paths.exports),
            log_level="INFO",
            traditional_chinese_output=True,
            speaker_mode="auto",
            speaker_count=2,
            diarization_device="auto",
            keep_display_on_during_recording=True,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        data = dict(data)
        original_version = data.get("schema_version")
        if original_version == 1:
            data["traditional_chinese_output"] = True
        if original_version in {1, 2}:
            data["speaker_mode"] = "auto"
            data["speaker_count"] = 2
            data["diarization_device"] = "auto"
        if original_version in {1, 2, 3, 4}:
            data["schema_version"] = SETTINGS_SCHEMA_VERSION
        if original_version in {1, 2, 3}:
            data["keep_display_on_during_recording"] = True
        required_fields = {
            "schema_version",
            "language",
            "whisper_model",
            "compute_device",
            "model_directory",
            "export_directory",
            "log_level",
            "traditional_chinese_output",
            "speaker_mode",
            "speaker_count",
            "diarization_device",
            "keep_display_on_during_recording",
        }
        missing = required_fields.difference(data)
        if missing:
            raise SettingsError(
                "設定檔缺少必要欄位：" + ", ".join(sorted(missing))
            )

        settings = cls(**{field: data[field] for field in required_fields})
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise SettingsError(
                f"不支援的設定版本：{self.schema_version}；"
                f"目前支援 {SETTINGS_SCHEMA_VERSION}。"
            )
        if not isinstance(self.language, str) or not self.language.strip():
            raise SettingsError("語言設定不可為空。")
        if not isinstance(self.whisper_model, str) or not self.whisper_model.strip():
            raise SettingsError("Whisper 模型名稱不可為空。")
        if self.compute_device not in SUPPORTED_COMPUTE_DEVICES:
            raise SettingsError(f"不支援的運算裝置：{self.compute_device}")
        if self.log_level not in SUPPORTED_LOG_LEVELS:
            raise SettingsError(f"不支援的日誌層級：{self.log_level}")
        if not isinstance(self.model_directory, str) or not self.model_directory.strip():
            raise SettingsError("模型資料夾不可為空。")
        if not isinstance(self.export_directory, str) or not self.export_directory.strip():
            raise SettingsError("匯出資料夾不可為空。")
        if not isinstance(self.traditional_chinese_output, bool):
            raise SettingsError("繁體中文輸出設定必須是布林值。")
        if self.speaker_mode not in SUPPORTED_SPEAKER_MODES:
            raise SettingsError(f"不支援的說話者模式：{self.speaker_mode}")
        if not isinstance(self.speaker_count, int) or not 2 <= self.speaker_count <= 6:
            raise SettingsError("指定說話者人數必須介於 2～6。")
        if self.diarization_device not in SUPPORTED_DIARIZATION_DEVICES:
            raise SettingsError(
                f"不支援的說話者分析裝置：{self.diarization_device}"
            )
        if not isinstance(self.keep_display_on_during_recording, bool):
            raise SettingsError("錄製期間保持螢幕開啟設定必須是布林值。")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


class SettingsStore:
    """Loads and atomically saves the application settings JSON file."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.path = paths.settings_file

    def load(self) -> AppSettings:
        try:
            raw_data = self.path.read_text(encoding="utf-8")
            data = json.loads(raw_data)
        except FileNotFoundError as exc:
            raise SettingsError(f"找不到設定檔：{self.path}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SettingsError(f"無法讀取設定檔：{self.path}") from exc

        if not isinstance(data, dict):
            raise SettingsError("設定檔根節點必須是 JSON 物件。")
        original_version = data.get("schema_version")
        settings = AppSettings.from_dict(data)
        raw_model_directory = settings.model_directory
        raw_export_directory = settings.export_directory
        settings.model_directory = str(
            self.paths.resolve_configured_path(settings.model_directory)
        )
        settings.export_directory = str(
            self.paths.resolve_configured_path(settings.export_directory)
        )
        normalized_model_directory = self.paths.serialize_configured_path(
            settings.model_directory
        )
        normalized_export_directory = self.paths.serialize_configured_path(
            settings.export_directory
        )
        if (
            original_version != SETTINGS_SCHEMA_VERSION
            or raw_model_directory != normalized_model_directory
            or raw_export_directory != normalized_export_directory
        ):
            self.save(settings)
        return settings

    def load_or_create(self) -> AppSettings:
        if self.path.exists():
            return self.load()
        settings = AppSettings.defaults(self.paths)
        self.save(settings)
        return settings

    def save(self, settings: AppSettings) -> None:
        settings.validate()
        self.paths.ensure()
        payload = settings.to_dict()
        payload["model_directory"] = self.paths.serialize_configured_path(
            settings.model_directory
        )
        payload["export_directory"] = self.paths.serialize_configured_path(
            settings.export_directory
        )
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_file.write(serialized)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
                temporary_path = Path(temporary_file.name)

            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise SettingsError(f"無法保存設定檔：{self.path}") from exc
