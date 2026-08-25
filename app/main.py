from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from app import __version__
from app.ai import (
    AIAnalysisError,
    DEFAULT_OPENAI_MODEL,
    OpenAIProvider,
    ProviderError,
    analyze_session,
)
from app.audio import (
    AudioBackendUnavailable,
    CaptureError,
    capture_loopback_to_wav,
    get_loopback_devices,
)
from app.audio.devices import LoopbackDeviceNotFound
from app.audio.test_tone import play_test_tone_async
from app.config import AppPaths, SettingsError, SettingsStore
from app.database import DatabaseError, SessionStore
from app.diagnostics import collect_diagnostics
from app.diarization import (
    DEFAULT_PYANNOTE_MODEL,
    DiarizationAudioError,
    DiarizationError,
    PyannoteSpeakerDiarizer,
    analyze_session_speakers,
    collect_diarization_runtime,
)
from app.exporting import (
    ExportError,
    SummaryExportError,
    export_session,
    export_summary,
)
from app.infrastructure import close_logging, configure_logging
from app.models import ModelManagementError, ModelManager
from app.stability import (
    StabilityObserver,
    build_failed_report,
    build_stability_report,
)
from app.text_normalization import (
    TextNormalizationError,
    build_text_normalizer,
)
from app.transcription import (
    BasicTranscriber,
    EngineLoadError,
    RecoveryError,
    StreamingTranscriptionError,
    TranscriptionError,
    WhisperEngine,
    recover_session_chunks,
    run_loopback_streaming_transcription,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-chapter-tool",
        description="Windows 長影片逐字稿與 AI 章節導覽工具",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="覆寫應用程式資料目錄，主要供開發與測試使用。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init", help="建立應用程式資料夾與預設設定。")

    diagnostics_parser = subparsers.add_parser(
        "diagnostics",
        help="收集不含逐字稿與密鑰的系統診斷資料。",
    )
    diagnostics_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出完整診斷資料。",
    )

    audio_devices_parser = subparsers.add_parser(
        "audio-devices",
        help="列出可擷取的 Windows WASAPI Loopback 裝置。",
    )
    audio_devices_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出裝置資料。",
    )

    capture_parser = subparsers.add_parser(
        "capture-audio",
        help="擷取有時間上限的系統音訊 WAV，供 Stage 1 驗證。",
    )
    capture_parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="擷取秒數，預設 5 秒，Prototype 上限 300 秒。",
    )
    capture_parser.add_argument(
        "--device-index",
        type=int,
        help="指定 Loopback 裝置索引；未指定時使用預設播放裝置。",
    )
    capture_parser.add_argument(
        "--output",
        type=Path,
        help="WAV 輸出位置；未指定時保存至應用程式 Cache。",
    )
    capture_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出擷取結果。",
    )
    capture_parser.add_argument(
        "--play-test-tone",
        action="store_true",
        help="串流啟動後播放約半秒低音量測試音，供端到端驗證。",
    )

    model_list_parser = subparsers.add_parser(
        "model-list",
        help="列出 Whisper 模型安裝與完整性狀態。",
    )
    model_list_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出模型狀態。",
    )

    model_download_parser = subparsers.add_parser(
        "model-download",
        help="下載並驗證 Whisper 模型。",
    )
    model_download_parser.add_argument(
        "name",
        choices=ModelManager.supported_model_names(),
        help="要下載的模型名稱。",
    )

    model_delete_parser = subparsers.add_parser(
        "model-delete",
        help="刪除已安裝的 Whisper 模型。",
    )
    model_delete_parser.add_argument(
        "name",
        choices=ModelManager.supported_model_names(),
        help="要刪除的模型名稱。",
    )
    model_delete_parser.add_argument(
        "--confirm",
        required=True,
        help="必須再次輸入完全相同的模型名稱。",
    )

    transcribe_parser = subparsers.add_parser(
        "transcribe-file",
        help="以已下載的 faster-whisper 模型轉錄音訊檔案。",
    )
    transcribe_parser.add_argument("source", type=Path, help="輸入音訊檔案。")
    transcribe_parser.add_argument(
        "--model",
        choices=ModelManager.supported_model_names(),
        help="模型名稱；未指定時使用設定檔。",
    )
    transcribe_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "amd_experimental"),
        help="運算裝置；未指定時使用設定檔。",
    )
    transcribe_parser.add_argument(
        "--language",
        help="語言代碼，例如 zh；使用 auto 代表自動偵測。",
    )
    transcribe_parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="搜尋 Beam Size，預設 5。",
    )
    transcribe_parser.add_argument(
        "--no-vad",
        action="store_true",
        help="停用 Silero VAD；預設啟用。",
    )
    transcribe_parser.add_argument(
        "--output",
        type=Path,
        help="JSON 輸出路徑；未指定時使用設定的匯出資料夾。",
    )

    stream_parser = subparsers.add_parser(
        "stream-transcribe",
        help="擷取系統音訊並以重疊 Chunk 串流轉錄。",
    )
    stream_parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="串流擷取秒數，預設 60 秒。",
    )
    stream_parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=25.0,
        help="每個音訊 Chunk 秒數，預設 25 秒。",
    )
    stream_parser.add_argument(
        "--overlap-seconds",
        type=float,
        default=2.0,
        help="相鄰 Chunk 重疊秒數，預設 2 秒。",
    )
    stream_parser.add_argument(
        "--device-index",
        type=int,
        help="指定 Loopback 裝置索引；未指定時使用預設播放裝置。",
    )
    stream_parser.add_argument(
        "--model",
        choices=ModelManager.supported_model_names(),
        help="模型名稱；未指定時使用設定檔。",
    )
    stream_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "amd_experimental"),
        help="運算裝置；未指定時使用設定檔。",
    )
    stream_parser.add_argument(
        "--language",
        help="語言代碼，例如 zh；使用 auto 代表自動偵測。",
    )
    stream_parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="搜尋 Beam Size，預設 5。",
    )
    stream_parser.add_argument(
        "--no-vad",
        action="store_true",
        help="停用 Silero VAD；預設啟用。",
    )
    stream_parser.add_argument(
        "--output",
        type=Path,
        help="額外輸出舊版單一 JSON；TXT/JSON/SRT 仍會匯出至工作資料夾。",
    )
    stream_parser.add_argument(
        "--title",
        help="工作階段與匯出資料夾標題。",
    )
    stream_parser.add_argument(
        "--platform",
        choices=("facebook", "instagram", "youtube", "web", "other"),
        help="來源平台，供工作階段紀錄使用。",
    )
    stream_parser.add_argument(
        "--source-label",
        help="來源說明，例如直播名稱；不會自動下載影片。",
    )
    stream_parser.add_argument(
        "--export-directory",
        type=Path,
        help="本次匯出根目錄；未指定時使用設定檔。",
    )
    stream_parser.add_argument(
        "--keep-chunks",
        action="store_true",
        help="成功後仍保留 PendingAudio Chunk，供開發診斷。",
    )
    stream_parser.add_argument(
        "--speakers",
        choices=("off", "auto", "1", "2", "3", "4", "5", "6"),
        default="off",
        help="說話者分析：off、auto（2～6 人）或指定 1～6 人；啟用時暫存 Chunk。",
    )
    stream_parser.add_argument(
        "--diarization-device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="pyannote 運算裝置，預設 auto。",
    )
    stream_parser.add_argument(
        "--diarization-model",
        default=DEFAULT_PYANNOTE_MODEL,
        help="pyannote 模型 ID 或本機模型資料夾。",
    )
    stream_parser.add_argument(
        "--diarization-cache-directory",
        type=Path,
        help="pyannote 模型快取位置；預設位於應用程式 Models。",
    )
    stream_parser.add_argument(
        "--keep-diarization-audio",
        action="store_true",
        help="分析成功後仍保留重建音訊與 Chunk，僅供開發診斷。",
    )
    stream_parser.add_argument(
        "--play-test-tone",
        action="store_true",
        help="串流開始後播放測試音，供端到端驗證。",
    )
    stream_parser.add_argument(
        "--stability-report",
        type=Path,
        help="穩定性 JSON 報告位置；未指定時保存至資料目錄。",
    )
    stream_parser.add_argument(
        "--memory-sample-seconds",
        type=float,
        default=5.0,
        help="程序 RAM 取樣間隔，預設 5 秒。",
    )

    session_list_parser = subparsers.add_parser(
        "session-list",
        help="列出 SQLite 工作階段與未完成工作。",
    )
    session_list_parser.add_argument(
        "--incomplete",
        action="store_true",
        help="只列出未完成、失敗或可重新匯出的工作。",
    )
    session_list_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出工作階段。",
    )

    session_export_parser = subparsers.add_parser(
        "session-export",
        help="從 SQLite 重新匯出 TXT、JSON 與 SRT。",
    )
    session_export_parser.add_argument("session_id", type=int)
    session_export_parser.add_argument(
        "--output-directory",
        type=Path,
        help="覆寫本次匯出根目錄。",
    )

    ai_export_parser = subparsers.add_parser(
        "ai-export",
        help="將已完成的 AI 摘要與章節匯出為 Markdown。",
    )
    ai_export_parser.add_argument("session_id", type=int)

    ai_analyze_parser = subparsers.add_parser(
        "ai-analyze",
        help="將 SQLite 逐字稿送至雲端模型，產生 AI 章節與摘要。",
    )
    ai_analyze_parser.add_argument("session_id", type=int)
    ai_analyze_parser.add_argument(
        "--provider",
        choices=("openai",),
        default="openai",
        help="AI 供應商；目前支援 openai。",
    )
    ai_analyze_parser.add_argument(
        "--model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"模型名稱，預設 {DEFAULT_OPENAI_MODEL}。",
    )
    ai_analyze_parser.add_argument(
        "--language",
        default="zh-TW",
        help="AI 摘要輸出語言，預設 zh-TW。",
    )
    ai_analyze_parser.add_argument(
        "--max-batch-chars",
        type=int,
        default=12_000,
        help="每批逐字稿文字上限，預設 12000 字元。",
    )
    ai_analyze_parser.add_argument(
        "--max-batch-seconds",
        type=float,
        default=900.0,
        help="每批最長時間範圍，預設 900 秒；不代表章節長度。",
    )

    session_recover_parser = subparsers.add_parser(
        "session-recover",
        help="重新處理 SQLite 登記但尚未完成的音訊 Chunk。",
    )
    session_recover_parser.add_argument("session_id", type=int)
    session_recover_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "amd_experimental"),
        help="覆寫原工作階段的運算裝置。",
    )

    speaker_analyze_parser = subparsers.add_parser(
        "speaker-analyze",
        help="使用保留的 Chunk 在本機分析並標註說話者。",
    )
    speaker_analyze_parser.add_argument("session_id", type=int)
    speaker_analyze_parser.add_argument(
        "--speakers",
        choices=("auto", "1", "2", "3", "4", "5", "6"),
        default="auto",
        help="自動判斷 2～6 人，或指定 1～6 人。",
    )
    speaker_analyze_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
    )
    speaker_analyze_parser.add_argument(
        "--model",
        default=DEFAULT_PYANNOTE_MODEL,
        help="pyannote 模型 ID 或本機模型資料夾。",
    )
    speaker_analyze_parser.add_argument("--cache-directory", type=Path)
    speaker_analyze_parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="成功後仍保留分析 WAV 與 Chunk。",
    )

    speaker_diagnostics_parser = subparsers.add_parser(
        "speaker-diagnostics",
        help="檢查 pyannote、Hugging Face Token 與 CUDA 執行環境。",
    )
    speaker_diagnostics_parser.add_argument("--json", action="store_true")

    speaker_model_parser = subparsers.add_parser(
        "speaker-model-download",
        help="下載並載入 pyannote 說話者模型。",
    )
    speaker_model_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
    )
    speaker_model_parser.add_argument(
        "--model",
        default=DEFAULT_PYANNOTE_MODEL,
    )
    speaker_model_parser.add_argument("--cache-directory", type=Path)

    speaker_show_parser = subparsers.add_parser(
        "speaker-show",
        help="顯示工作階段的說話者分析狀態與名稱。",
    )
    speaker_show_parser.add_argument("session_id", type=int)
    speaker_show_parser.add_argument("--json", action="store_true")

    speaker_rename_parser = subparsers.add_parser(
        "speaker-rename",
        help="重新命名匿名說話者並重新匯出逐字稿。",
    )
    speaker_rename_parser.add_argument("session_id", type=int)
    speaker_rename_parser.add_argument("speaker_id")
    speaker_rename_parser.add_argument("display_name")
    session_recover_parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="搜尋 Beam Size，預設 5。",
    )
    session_recover_parser.add_argument(
        "--output-directory",
        type=Path,
        help="復原完成後的匯出根目錄。",
    )

    stability_show_parser = subparsers.add_parser(
        "stability-show",
        help="顯示工作階段的穩定性 Gate 報告。",
    )
    stability_show_parser.add_argument("session_id", type=int)
    stability_show_parser.add_argument(
        "--json",
        action="store_true",
        help="輸出完整 JSON 報告。",
    )
    return parser


def _resolve_paths(data_dir: Path | None) -> AppPaths:
    if data_dir is not None:
        return AppPaths.from_root(data_dir)
    return AppPaths.default()


def _print_human_diagnostics(report: dict[str, object]) -> None:
    operating_system = report["operating_system"]
    python = report["python"]
    cpu = report["cpu"]
    memory = report["memory"]
    gpus = report["gpus"]
    audio = report["audio_controllers"]
    warnings = report["warnings"]

    print("Video Chapter Tool 系統診斷")
    print(f"作業系統：{operating_system}")
    print(f"Python：{python}")
    print(f"CPU：{cpu}")
    print(f"記憶體：{memory}")
    print(f"顯示裝置：{gpus if gpus else '未偵測到'}")
    print(f"音訊硬體：{audio if audio else '未偵測到'}")
    if warnings:
        print("警告：")
        for warning in warnings:
            print(f"- {warning}")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    paths = _resolve_paths(args.data_dir)
    try:
        paths.ensure()
        settings = SettingsStore(paths).load_or_create()
    except (OSError, SettingsError) as exc:
        parser.exit(2, f"初始化失敗：{exc}\n")

    logger = configure_logging(paths, settings.log_level, console=False)
    try:
        logger.info("Application command started: %s", args.command)
        try:
            session_store = SessionStore(paths.database_file)
            incomplete_count = len(
                session_store.list_sessions(incomplete_only=True)
            )
        except DatabaseError as exc:
            logger.error("Database initialization failed: %s", type(exc).__name__)
            print(f"資料庫初始化失敗：{exc}")
            return 12
        if incomplete_count:
            logger.info("Detected %d recoverable session(s)", incomplete_count)

        if args.command == "init":
            print("初始化完成。")
            print(f"應用程式資料：{paths.root}")
            print(f"設定檔：{paths.settings_file}")
            print(f"SQLite：{paths.database_file}")
            print(f"模型資料夾：{settings.model_directory}")
            print(f"預設匯出資料夾：{settings.export_directory}")
            return 0

        if args.command == "diagnostics":
            report = collect_diagnostics(paths)
            logger.info(
                "Diagnostics completed with %d warning(s)",
                len(report["warnings"]),
            )
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                _print_human_diagnostics(report)
            return 0

        if args.command == "audio-devices":
            try:
                devices = get_loopback_devices()
            except (AudioBackendUnavailable, LoopbackDeviceNotFound) as exc:
                print(f"音訊裝置偵測失敗：{exc}")
                return 3
            device_data = [device.to_dict() for device in devices]
            logger.info("Detected %d WASAPI loopback device(s)", len(devices))
            if args.json:
                print(json.dumps(device_data, ensure_ascii=False, indent=2))
            else:
                print("Windows WASAPI Loopback 裝置：")
                for device in devices:
                    default_marker = " [預設]" if device.is_default else ""
                    print(
                        f"- #{device.index} {device.name}{default_marker} | "
                        f"{device.channels}ch {device.sample_rate}Hz"
                    )
            return 0

        if args.command == "capture-audio":
            output_path = args.output
            if output_path is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = paths.cache / f"loopback_{timestamp}.wav"
            try:
                result = capture_loopback_to_wav(
                    duration_seconds=args.duration,
                    output_path=output_path,
                    device_index=args.device_index,
                    on_stream_started=(
                        play_test_tone_async
                        if args.play_test_tone
                        else None
                    ),
                )
            except (
                AudioBackendUnavailable,
                CaptureError,
                LoopbackDeviceNotFound,
                ValueError,
            ) as exc:
                logger.error("Bounded audio capture failed: %s", exc)
                print(f"音訊擷取失敗：{exc}")
                return 4
            logger.info(
                "Bounded audio capture completed: frames=%d duration=%.3f",
                result.frames_captured,
                result.captured_duration_seconds,
            )
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            else:
                print("短片段音訊擷取完成。")
                print(f"裝置：{result.device.name}")
                print(f"PCM 時長：{result.captured_duration_seconds:.3f} 秒")
                print(f"實收 Frame：{result.source_frames_received}")
                print(f"補靜音 Frame：{result.silence_frames_padded}")
                print(f"峰值：{result.peak_level:.4f}")
                print(f"RMS：{result.rms_level:.4f}")
                print(f"輸出：{result.output_path}")
            return 0

        if args.command == "model-list":
            manager = ModelManager(settings.model_directory)
            statuses = manager.list_statuses()
            if args.json:
                print(
                    json.dumps(
                        [status.to_dict() for status in statuses],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(f"模型資料夾：{manager.root}")
                for status in statuses:
                    if status.valid:
                        state = f"可用，{status.size_bytes / 1024**2:.1f} MiB"
                    elif status.installed:
                        state = "不完整：" + ", ".join(status.missing_files)
                    elif status.partial_download_exists:
                        state = "下載未完成，可繼續"
                    else:
                        state = "尚未下載"
                    print(f"- {status.name}: {state}")
            return 0

        if args.command == "model-download":
            manager = ModelManager(settings.model_directory)
            print(f"準備下載 {args.name} 至：{manager.model_path(args.name)}")
            try:
                status = manager.download(args.name)
            except ModelManagementError as exc:
                logger.error("Model download failed: %s", type(exc).__name__)
                print(f"模型下載失敗：{exc}")
                return 5
            logger.info(
                "Model ready: name=%s size_bytes=%d",
                status.name,
                status.size_bytes,
            )
            print(f"模型驗證完成：{status.path}")
            print(f"模型大小：{status.size_bytes / 1024**2:.1f} MiB")
            return 0

        if args.command == "model-delete":
            manager = ModelManager(settings.model_directory)
            try:
                deleted = manager.delete(args.name, confirmation=args.confirm)
            except ModelManagementError as exc:
                print(f"模型刪除失敗：{exc}")
                return 6
            print("模型已刪除。" if deleted else "模型尚未安裝。")
            return 0

        if args.command == "session-list":
            try:
                sessions = session_store.list_sessions(
                    incomplete_only=args.incomplete
                )
            except DatabaseError as exc:
                print(f"工作階段讀取失敗：{exc}")
                return 13
            if args.json:
                print(
                    json.dumps(
                        [session.to_dict() for session in sessions],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif not sessions:
                print("沒有符合條件的工作階段。")
            else:
                print("SQLite 工作階段：")
                for session in sessions:
                    print(
                        f"- #{session.id} [{session.status}] {session.title} | "
                        f"{session.duration_seconds:.1f} 秒 | {session.created_at}"
                    )
                    if session.error_message:
                        print(f"  錯誤：{session.error_message}")
                    if session.exported_path:
                        print(f"  匯出：{session.exported_path}")
                    if session.stability_report_path:
                        print(f"  穩定性報告：{session.stability_report_path}")
            return 0

        if args.command == "session-export":
            try:
                export_result = export_session(
                    session_store,
                    args.session_id,
                    output_root=args.output_directory,
                )
                session_store.update_status(args.session_id, "completed")
            except (DatabaseError, ExportError, ValueError) as exc:
                logger.error("Session export failed: %s", type(exc).__name__)
                print(f"工作階段匯出失敗：{exc}")
                return 14
            print("工作階段匯出完成。")
            print(f"資料夾：{export_result.directory}")
            print(f"TXT：{export_result.txt_path}")
            print(f"JSON：{export_result.json_path}")
            print(f"SRT：{export_result.srt_path}")
            return 0

        if args.command == "ai-export":
            try:
                summary_path = export_summary(session_store, args.session_id)
            except (DatabaseError, ExportError, SummaryExportError) as exc:
                logger.error("AI summary export failed: %s", type(exc).__name__)
                print(f"AI 摘要匯出失敗：{exc}")
                return 19
            print("AI 摘要匯出完成。")
            print(f"Markdown：{summary_path}")
            return 0

        if args.command == "ai-analyze":
            try:
                provider = OpenAIProvider(model_name=args.model)
                print("AI 分析已開始；只會傳送逐字稿文字與時間戳，不傳送音訊。")
                analysis = analyze_session(
                    session_store,
                    args.session_id,
                    provider,
                    language=args.language,
                    max_chars=args.max_batch_chars,
                    max_seconds=args.max_batch_seconds,
                    retry_attempts=3,
                    retry_delay_seconds=2.0,
                )
                summary_path = export_summary(session_store, args.session_id)
            except (
                AIAnalysisError,
                DatabaseError,
                ProviderError,
                SummaryExportError,
                ValueError,
            ) as exc:
                logger.error("AI analysis failed: %s", type(exc).__name__)
                print(f"AI 章節分析失敗：{exc}")
                return 20
            print("AI 章節分析完成。")
            print(f"供應商／模型：{analysis.provider_name}/{analysis.model_name}")
            print(f"文字批次：{analysis.batches_total}")
            print(f"沿用成功快取：{analysis.batches_reused}")
            print(f"本次 API 呼叫：{analysis.provider_calls}")
            print(f"最終章節：{len(analysis.chapters)}")
            print(f"Markdown：{summary_path}")
            return 0

        if args.command == "speaker-diagnostics":
            cache_directory = Path(settings.model_directory) / "Diarization"
            report = collect_diarization_runtime(cache_directory)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print("說話者分析執行環境：")
                print(f"- pyannote.audio：{report['pyannote_audio'] or '未安裝'}")
                print(f"- PyTorch：{report['torch'] or '未安裝'}")
                print(
                    f"- CUDA：{report['cuda_runtime'] or '不可用'} | "
                    f"{report['gpu_name'] or '未偵測到 GPU'}"
                )
                print(
                    "- CUDA 實測："
                    + ("pass" if report["ready_for_cuda"] else "fail")
                )
                print(
                    "- Hugging Face Token："
                    + ("已設定" if report["hf_token_present"] else "未設定")
                )
                print(f"- 模型目錄：{report['cache_directory']}")
                if report["error"]:
                    print(f"- 錯誤：{report['error']}")
            return 0 if report["ready_for_cpu"] else 21

        if args.command == "speaker-model-download":
            cache_directory = (
                args.cache_directory
                if args.cache_directory is not None
                else Path(settings.model_directory) / "Diarization"
            )
            try:
                diarizer = PyannoteSpeakerDiarizer(
                    model_source=args.model,
                    cache_directory=cache_directory,
                    requested_device=args.device,
                )
                print("正在下載／載入本機說話者模型；首次執行可能需要數分鐘。")
                resolved_device = diarizer.prepare()
            except (DiarizationError, OSError, ValueError) as exc:
                logger.error("Speaker model preparation failed: %s", type(exc).__name__)
                print(f"說話者模型準備失敗：{exc}")
                return 21
            print("說話者模型準備完成。")
            print(f"模型：{args.model}")
            print(f"運算：{resolved_device}")
            print(f"模型目錄：{cache_directory.expanduser().resolve()}")
            return 0

        if args.command == "speaker-analyze":
            speaker_count = (
                None if args.speakers == "auto" else int(args.speakers)
            )
            cache_directory = (
                args.cache_directory
                if args.cache_directory is not None
                else Path(settings.model_directory) / "Diarization"
            )
            try:
                diarizer = PyannoteSpeakerDiarizer(
                    model_source=args.model,
                    cache_directory=cache_directory,
                    requested_device=args.device,
                )
                print(
                    "本機說話者分析已開始；音訊不會傳送至 OpenAI，"
                    "pyannote community 模型在本機執行。"
                )
                diarization = analyze_session_speakers(
                    session_store,
                    args.session_id,
                    diarizer,
                    working_directory=paths.cache / "Diarization",
                    speaker_count=speaker_count,
                    cleanup_audio=not args.keep_audio,
                )
                export_result = export_session(session_store, args.session_id)
            except (
                DatabaseError,
                DiarizationAudioError,
                DiarizationError,
                ExportError,
                OSError,
                ValueError,
            ) as exc:
                logger.error("Speaker diarization failed: %s", type(exc).__name__)
                print(f"說話者分析失敗：{exc}")
                print("逐字稿仍保存在 SQLite，保留的 Chunk 可供重試。")
                return 21
            labels = session_store.get_speaker_labels(args.session_id)
            print("說話者分析完成。")
            print(
                f"供應商／模型：{diarization.result.provider_name}/"
                f"{diarization.result.model_name}"
            )
            print(f"運算：{diarization.result.resolved_device}")
            print(f"說話者：{len(labels)}")
            print(f"時間區段：{len(diarization.result.turns)}")
            print(f"已清理 Chunk：{diarization.chunks_cleaned}")
            print(f"匯出資料夾：{export_result.directory}")
            if args.keep_audio:
                print(f"分析 WAV：{diarization.audio_path}")
            return 0

        if args.command == "speaker-show":
            try:
                session = session_store.get_session(args.session_id)
                labels = session_store.get_speaker_labels(args.session_id)
                turns = session_store.get_speaker_turns(args.session_id)
            except DatabaseError as exc:
                print(f"無法讀取說話者分析：{exc}")
                return 21
            payload = {
                "session_id": session.id,
                "status": session.diarization_status,
                "provider": session.diarization_provider,
                "model": session.diarization_model,
                "requested_speaker_count": session.requested_speaker_count,
                "error": session.diarization_error,
                "labels": [item.to_dict() for item in labels],
                "turn_count": len(turns),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"說話者分析：{payload['status'] or '尚未執行'}")
                if session.diarization_provider:
                    print(
                        f"供應商／模型：{session.diarization_provider}/"
                        f"{session.diarization_model}"
                    )
                print(f"時間區段：{len(turns)}")
                for label in labels:
                    print(f"- {label.speaker_id}：{label.display_name}")
                if session.diarization_error:
                    print(f"錯誤：{session.diarization_error}")
            return 0

        if args.command == "speaker-rename":
            try:
                session_store.rename_speaker(
                    args.session_id,
                    args.speaker_id,
                    args.display_name,
                )
                export_result = export_session(session_store, args.session_id)
            except (DatabaseError, ExportError, ValueError) as exc:
                print(f"說話者重新命名失敗：{exc}")
                return 21
            print("說話者重新命名完成。")
            print(f"匯出資料夾：{export_result.directory}")
            return 0

        if args.command == "stability-show":
            try:
                stability_session = session_store.get_session(args.session_id)
                if not stability_session.stability_report_path:
                    raise DatabaseError(
                        f"工作階段 {args.session_id} 尚無穩定性報告。"
                    )
                stability_path = Path(stability_session.stability_report_path)
                stability_payload = json.loads(
                    stability_path.read_text(encoding="utf-8")
                )
            except (
                DatabaseError,
                OSError,
                UnicodeError,
                json.JSONDecodeError,
            ) as exc:
                print(f"無法讀取穩定性報告：{exc}")
                return 18
            if args.json:
                print(json.dumps(stability_payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"穩定性 Gate：{stability_payload.get('outcome', 'unknown')}"
                )
                print(f"層級：{stability_payload.get('gate_level', 'unknown')}")
                print(f"報告：{stability_path}")
                checks = stability_payload.get("checks", {})
                if isinstance(checks, dict):
                    for name, check in checks.items():
                        if isinstance(check, dict):
                            print(
                                f"- {name}: {check.get('status', 'unknown')} | "
                                f"{check.get('detail', '')}"
                            )
            return 0

        if args.command == "session-recover":
            try:
                database_session = session_store.get_session(args.session_id)
                normalize_text = build_text_normalizer(
                    language=database_session.language,
                    traditional_chinese_output=(
                        settings.traditional_chinese_output
                    ),
                )
                manager = ModelManager(settings.model_directory)
                status = manager.status(database_session.model_name)
                if not status.valid:
                    print(
                        f"模型 {database_session.model_name} 尚未完成下載或驗證。"
                    )
                    return 7
                requested_device = args.device or database_session.requested_device
                engine = WhisperEngine.load(
                    manager.model_path(database_session.model_name),
                    requested_device=requested_device,
                )

                def build_recovery_cpu_fallback(
                    cuda_error: BaseException,
                ) -> BasicTranscriber | None:
                    if engine.runtime.resolved_device != "cuda":
                        return None
                    cpu_engine = WhisperEngine.load(
                        manager.model_path(database_session.model_name),
                        requested_device="cpu",
                    )
                    cpu_engine.runtime = replace(
                        cpu_engine.runtime,
                        requested_device=requested_device,
                        fallback_reason=(
                            "CUDA 復原推論失敗，已改用 CPU INT8："
                            + " ".join(str(cuda_error).split())[:240]
                        ),
                    )
                    return BasicTranscriber(cpu_engine)

                recovery = recover_session_chunks(
                    session_store,
                    database_session,
                    BasicTranscriber(engine),
                    beam_size=args.beam_size,
                    fallback_transcriber_factory=build_recovery_cpu_fallback,
                    normalize_text=normalize_text,
                    retain_completed_audio=(
                        database_session.diarization_status
                        in {"pending", "analyzing", "failed"}
                    ),
                    word_timestamps=(
                        database_session.diarization_status
                        in {"pending", "analyzing", "failed"}
                    ),
                )
                export_result = export_session(
                    session_store,
                    database_session.id,
                    output_root=args.output_directory,
                )
                session_store.update_status(database_session.id, "completed")
            except (
                DatabaseError,
                EngineLoadError,
                ExportError,
                RecoveryError,
                TextNormalizationError,
                ValueError,
            ) as exc:
                try:
                    session_store.update_status(
                        args.session_id,
                        "failed",
                        error_message=" ".join(str(exc).split())[:500],
                    )
                except DatabaseError:
                    pass
                logger.error("Session recovery failed: %s", type(exc).__name__)
                print(f"工作階段復原失敗：{exc}")
                return 15
            print("工作階段復原完成。")
            print(f"處理 Chunk：{recovery.chunks_processed}")
            print(f"新增片段：{recovery.segments_added}")
            print(f"剩餘 Chunk：{recovery.remaining_chunks}")
            print(f"匯出資料夾：{export_result.directory}")
            return 0

        if args.command == "stream-transcribe":
            model_name = args.model or settings.whisper_model
            requested_device = args.device or settings.compute_device
            configured_language = args.language or settings.language
            language = None if configured_language == "auto" else configured_language
            speaker_enabled = args.speakers != "off"
            speaker_count = (
                None
                if args.speakers in {"off", "auto"}
                else int(args.speakers)
            )
            try:
                normalize_text = build_text_normalizer(
                    language=language,
                    traditional_chinese_output=(
                        settings.traditional_chinese_output
                    ),
                )
            except TextNormalizationError as exc:
                print(f"文字正規化初始化失敗：{exc}")
                return 16
            manager = ModelManager(settings.model_directory)
            status = manager.status(model_name)
            if not status.valid:
                print(
                    f"模型 {model_name} 尚未完成下載或驗證；"
                    f"請先執行 model-download {model_name}。"
                )
                return 7

            session_title = args.title or datetime.now().strftime(
                "串流轉錄 %Y-%m-%d %H-%M-%S"
            )
            export_root = (
                args.export_directory
                if args.export_directory is not None
                else Path(settings.export_directory)
            )
            try:
                database_session = session_store.create_session(
                    title=session_title,
                    platform=args.platform,
                    source=args.source_label,
                    language=language,
                    model_name=model_name,
                    requested_device=requested_device,
                    export_directory=export_root,
                    chunk_seconds=args.chunk_seconds,
                    overlap_seconds=args.overlap_seconds,
                )
            except (DatabaseError, ValueError) as exc:
                print(f"無法建立 SQLite 工作階段：{exc}")
                return 13
            if speaker_enabled:
                try:
                    session_store.set_diarization_status(
                        database_session.id,
                        "pending",
                        provider_name="pyannote",
                        model_name=args.diarization_model,
                        requested_speaker_count=speaker_count,
                    )
                except (DatabaseError, ValueError) as exc:
                    session_store.update_status(
                        database_session.id,
                        "failed",
                        error_message=str(exc),
                    )
                    print(f"無法建立說話者分析工作：{exc}")
                    return 21
            spool_directory = (
                paths.pending_audio / f"session_{database_session.id}"
            )
            report_path = (
                args.stability_report
                if args.stability_report is not None
                else paths.stability_reports
                / f"session_{database_session.id}_stability.json"
            )
            try:
                observer = StabilityObserver(
                    sample_interval_seconds=args.memory_sample_seconds,
                )
            except ValueError as exc:
                session_store.update_status(
                    database_session.id,
                    "failed",
                    error_message=str(exc),
                )
                print(f"穩定性監控設定錯誤：{exc}")
                return 17
            try:
                engine = WhisperEngine.load(
                    manager.model_path(model_name),
                    requested_device=requested_device,
                )
                session_store.update_runtime(
                    database_session.id,
                    resolved_device=engine.runtime.resolved_device,
                    compute_type=engine.runtime.compute_type,
                )
                session_store.update_status(database_session.id, "capturing")
                transcriber = BasicTranscriber(engine)
                observer.start()

                def build_cpu_fallback(
                    cuda_error: BaseException,
                ) -> BasicTranscriber | None:
                    if engine.runtime.resolved_device != "cuda":
                        return None
                    logger.warning(
                        "CUDA streaming inference failed; retrying on CPU: %s",
                        type(cuda_error).__name__,
                    )
                    cpu_engine = WhisperEngine.load(
                        manager.model_path(model_name),
                        requested_device="cpu",
                    )
                    fallback_summary = " ".join(str(cuda_error).split())[:240]
                    cpu_engine.runtime = replace(
                        cpu_engine.runtime,
                        requested_device=requested_device,
                        fallback_reason=(
                            "CUDA 串流推論失敗，已改用 CPU INT8："
                            + fallback_summary
                        ),
                    )
                    return BasicTranscriber(cpu_engine)

                def persist_spooled_chunk(chunk: Any) -> None:
                    session_store.register_chunk(database_session.id, chunk)

                def persist_transcribed_chunk(
                    chunk: Any,
                    segments: Any,
                ) -> None:
                    session_store.commit_chunk_transcript(
                        database_session.id,
                        chunk,
                        segments,
                        normalize_text=normalize_text,
                    )
                    if not args.keep_chunks and not speaker_enabled:
                        try:
                            chunk.path.unlink(missing_ok=True)
                        except OSError as cleanup_error:
                            logger.warning(
                                "Completed chunk cleanup failed: %s",
                                type(cleanup_error).__name__,
                            )

                def persist_chunk_failure(
                    chunk: Any,
                    chunk_error: BaseException,
                ) -> None:
                    session_store.mark_chunk_failed(
                        database_session.id,
                        chunk.index,
                        " ".join(str(chunk_error).split())[:500],
                    )

                def mark_capture_complete(capture: Any) -> None:
                    session_store.update_status(
                        database_session.id,
                        "draining_queue",
                        duration_seconds=capture.captured_duration_seconds,
                    )

                def report_stream_progress(progress: Any) -> None:
                    observer.record_progress(progress)
                    logger.info(
                        "Streaming progress: produced=%d processed=%d "
                        "queued=%d backlog_seconds=%.3f",
                        progress.chunks_produced,
                        progress.chunks_processed,
                        progress.queued_chunks,
                        progress.estimated_backlog_seconds,
                    )

                def announce_stream_started() -> None:
                    print("音訊擷取已開始，現在可以播放測試影片。", flush=True)
                    if args.play_test_tone:
                        play_test_tone_async()

                run = run_loopback_streaming_transcription(
                    transcriber,
                    duration_seconds=args.duration,
                    spool_directory=spool_directory,
                    language=language,
                    device_index=args.device_index,
                    chunk_seconds=args.chunk_seconds,
                    overlap_seconds=args.overlap_seconds,
                    beam_size=args.beam_size,
                    vad_filter=not args.no_vad,
                    word_timestamps=speaker_enabled,
                    on_stream_started=announce_stream_started,
                    on_progress=report_stream_progress,
                    on_chunk_spooled=persist_spooled_chunk,
                    on_chunk_transcribed=persist_transcribed_chunk,
                    on_chunk_failed=persist_chunk_failure,
                    on_capture_complete=mark_capture_complete,
                    fallback_transcriber_factory=build_cpu_fallback,
                )
                observer.stop()
                if speaker_enabled:
                    # CTranslate2 and pyannote use separate CUDA allocators.
                    # Drop the Whisper model before loading the diarization
                    # pipeline so an 8 GB GPU never has to retain both models.
                    engine.close()
                    del transcriber
                    gc.collect()
            except (
                AudioBackendUnavailable,
                CaptureError,
                EngineLoadError,
                LoopbackDeviceNotFound,
                StreamingTranscriptionError,
                DatabaseError,
                ValueError,
            ) as exc:
                observer.stop()
                logger.error("Streaming transcription failed: %s", type(exc).__name__)
                error_summary = " ".join(str(exc).split())[:500]
                try:
                    saved_report_path = _write_json_atomic(
                        report_path,
                        build_failed_report(
                            session_id=database_session.id,
                            observer=observer,
                            error=exc,
                        ),
                    )
                    session_store.set_stability_report_path(
                        database_session.id,
                        saved_report_path,
                    )
                except (DatabaseError, OSError):
                    saved_report_path = None
                try:
                    session_store.update_status(
                        database_session.id,
                        "failed",
                        error_message=error_summary,
                    )
                except DatabaseError:
                    pass
                print(f"串流轉錄失敗：{exc}")
                print(f"SQLite 工作階段：#{database_session.id}")
                print(f"待處理音訊：{spool_directory}")
                if saved_report_path is not None:
                    print(f"穩定性報告：{saved_report_path}")
                return 10

            try:
                pending_audio_files = sum(
                    1 for path in spool_directory.glob("*.wav") if path.is_file()
                )
                stability_report = build_stability_report(
                    session_id=database_session.id,
                    transcript=run.transcript,
                    observer=observer,
                    pending_audio_files=pending_audio_files,
                    cleanup_expected=not args.keep_chunks and not speaker_enabled,
                )
                saved_report_path = _write_json_atomic(
                    report_path,
                    stability_report,
                )
                session_store.set_stability_report_path(
                    database_session.id,
                    saved_report_path,
                )
                session_store.update_runtime(
                    database_session.id,
                    resolved_device=run.transcript.runtime["resolved_device"],
                    compute_type=run.transcript.runtime["compute_type"],
                )
                session_store.update_status(
                    database_session.id,
                    "transcript_ready",
                    duration_seconds=run.transcript.capture.captured_duration_seconds,
                )
                export_result = export_session(
                    session_store,
                    database_session.id,
                    output_root=export_root,
                )
                legacy_json_path = None
                if args.output is not None:
                    legacy_json_path = _write_json_atomic(
                        args.output,
                        run.transcript.to_dict(),
                    )
                diarization_result = None
                if speaker_enabled:
                    cache_directory = (
                        args.diarization_cache_directory
                        if args.diarization_cache_directory is not None
                        else Path(settings.model_directory) / "Diarization"
                    )
                    diarizer = PyannoteSpeakerDiarizer(
                        model_source=args.diarization_model,
                        cache_directory=cache_directory,
                        requested_device=args.diarization_device,
                    )
                    print("逐字稿完成，開始本機說話者分析。", flush=True)
                    try:
                        diarization_result = analyze_session_speakers(
                            session_store,
                            database_session.id,
                            diarizer,
                            working_directory=paths.cache / "Diarization",
                            speaker_count=speaker_count,
                            cleanup_audio=not (
                                args.keep_diarization_audio or args.keep_chunks
                            ),
                        )
                    except (
                        DiarizationAudioError,
                        DiarizationError,
                        DatabaseError,
                        ExportError,
                        OSError,
                        ValueError,
                    ) as diarization_error:
                        session_store.update_status(
                            database_session.id,
                            "transcript_ready",
                            duration_seconds=(
                                run.transcript.capture.captured_duration_seconds
                            ),
                            error_message=(
                                "說話者分析待重試："
                                + " ".join(str(diarization_error).split())[:450]
                            ),
                        )
                        print(f"說話者分析失敗：{diarization_error}")
                        print(
                            "逐字稿已匯出且 Chunk 已保留；安裝或修正 pyannote 後，"
                            f"可執行 speaker-analyze {database_session.id} 重試。"
                        )
                        return 21
                session_store.update_status(
                    database_session.id,
                    (
                        "cancelled"
                        if run.transcript.capture.cancelled_by_user
                        else "completed"
                    ),
                    duration_seconds=run.transcript.capture.captured_duration_seconds,
                )
                pending_audio_files = sum(
                    1
                    for path in spool_directory.glob("*.wav")
                    if path.is_file()
                )
                stability_report = build_stability_report(
                    session_id=database_session.id,
                    transcript=run.transcript,
                    observer=observer,
                    pending_audio_files=pending_audio_files,
                    cleanup_expected=(
                        not args.keep_chunks
                        and not (
                            speaker_enabled and args.keep_diarization_audio
                        )
                    ),
                )
                saved_report_path = _write_json_atomic(
                    report_path,
                    stability_report,
                )
                session_store.set_stability_report_path(
                    database_session.id,
                    saved_report_path,
                )
                export_result = export_session(
                    session_store,
                    database_session.id,
                    output_root=export_root,
                )
            except (DatabaseError, ExportError, OSError) as exc:
                logger.error(
                    "Streaming transcript persistence/export failed: %s",
                    type(exc).__name__,
                )
                try:
                    session_store.update_status(
                        database_session.id,
                        "transcript_ready",
                        duration_seconds=run.transcript.capture.captured_duration_seconds,
                        error_message=" ".join(str(exc).split())[:500],
                    )
                except DatabaseError:
                    pass
                print(
                    "逐字稿已保存在 SQLite，但匯出失敗；"
                    f"可使用 session-export {database_session.id} 重試：{exc}"
                )
                return 11

            if not args.keep_chunks and not speaker_enabled:
                run.cleanup_chunks()
            logger.info(
                "Streaming transcription completed: chunks=%d segments=%d",
                run.transcript.chunks_processed,
                len(run.transcript.segments),
            )
            if run.transcript.capture.cancelled_by_user:
                print("串流轉錄已由使用者提前停止，已保存完成內容。")
            else:
                print("串流轉錄完成。")
            print(f"SQLite 工作階段：#{database_session.id}")
            print(
                f"運算：{run.transcript.runtime['resolved_device']}/"
                f"{run.transcript.runtime['compute_type']}"
            )
            print(f"擷取時長：{run.transcript.capture.captured_duration_seconds:.3f} 秒")
            print(f"Chunk：{run.transcript.chunks_processed}")
            print(f"片段：{len(run.transcript.segments)}")
            print(
                "最高待處理量："
                f"{run.transcript.peak_estimated_backlog_seconds:.3f} 秒"
            )
            print(f"匯出資料夾：{export_result.directory}")
            print(f"TXT：{export_result.txt_path}")
            print(f"JSON：{export_result.json_path}")
            print(f"SRT：{export_result.srt_path}")
            print(
                f"穩定性 Gate：{stability_report['outcome']} | "
                f"報告：{saved_report_path}"
            )
            if legacy_json_path is not None:
                print(f"相容 JSON：{legacy_json_path}")
            if args.keep_chunks:
                print(f"Chunk 保留於：{spool_directory}")
            if diarization_result is not None:
                print(
                    "說話者分析："
                    f"{len(diarization_result.result.turns)} 個時間區段 | "
                    f"{diarization_result.result.resolved_device}"
                )
                if args.keep_diarization_audio:
                    print(f"分析 WAV：{diarization_result.audio_path}")
            return 0

        if args.command == "transcribe-file":
            model_name = args.model or settings.whisper_model
            requested_device = args.device or settings.compute_device
            configured_language = args.language or settings.language
            language = None if configured_language == "auto" else configured_language
            manager = ModelManager(settings.model_directory)
            status = manager.status(model_name)
            if not status.valid:
                print(
                    f"模型 {model_name} 尚未完成下載或驗證；"
                    f"請先執行 model-download {model_name}。"
                )
                return 7

            try:
                engine = WhisperEngine.load(
                    manager.model_path(model_name),
                    requested_device=requested_device,
                )
                try:
                    result = BasicTranscriber(engine).transcribe_file(
                        args.source,
                        language=language,
                        beam_size=args.beam_size,
                        vad_filter=not args.no_vad,
                    )
                except TranscriptionError as cuda_error:
                    if engine.runtime.resolved_device != "cuda":
                        raise
                    logger.warning(
                        "CUDA transcription failed; retrying on CPU: %s",
                        type(cuda_error).__name__,
                    )
                    cpu_engine = WhisperEngine.load(
                        manager.model_path(model_name),
                        requested_device="cpu",
                    )
                    fallback_summary = " ".join(str(cuda_error).split())[:240]
                    cpu_engine.runtime = replace(
                        cpu_engine.runtime,
                        requested_device=requested_device,
                        fallback_reason=(
                            "CUDA 推論失敗，已改用 CPU INT8："
                            + fallback_summary
                        ),
                    )
                    result = BasicTranscriber(cpu_engine).transcribe_file(
                        args.source,
                        language=language,
                        beam_size=args.beam_size,
                        vad_filter=not args.no_vad,
                    )
            except (EngineLoadError, TranscriptionError, ValueError) as exc:
                logger.error("File transcription failed: %s", type(exc).__name__)
                print(f"轉錄失敗：{exc}")
                return 8

            output_path = args.output
            if output_path is None:
                export_root = Path(settings.export_directory)
                output_path = export_root / f"{args.source.stem}.transcript.json"
            try:
                saved_path = _write_json_atomic(output_path, result.to_dict())
            except OSError as exc:
                logger.error("Transcript JSON save failed: %s", type(exc).__name__)
                print(f"逐字稿保存失敗：{exc}")
                return 9

            logger.info(
                "File transcription completed: segments=%d runtime=%s",
                len(result.segments),
                result.runtime.resolved_device,
            )
            print("基本轉錄完成。")
            print(
                f"運算：{result.runtime.resolved_device}/"
                f"{result.runtime.compute_type}"
            )
            if result.runtime.fallback_reason:
                print(f"回退原因：{result.runtime.fallback_reason}")
            print(f"語言：{result.language}")
            print(f"片段：{len(result.segments)}")
            print(f"處理時間：{result.transcription_seconds:.3f} 秒")
            print(f"輸出：{saved_path}")
            return 0

        logging.getLogger(__name__).error("Unhandled command: %s", args.command)
        return 2
    finally:
        close_logging(logger)


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
