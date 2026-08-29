from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app import __version__
from app.config import AppPaths, SettingsError, SettingsStore
from app.database import DatabaseError
from app.gui.main_window import MainWindow
from app.gui.resources import gui_asset_path
from app.infrastructure import close_logging, configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="直播影片摘要工具 Windows GUI")
    parser.add_argument(
        "--data-dir",
        type=str,
        help=(
            "覆寫應用程式資料根目錄；Portable EXE 未指定時使用程式旁的 "
            "UserData。"
        ),
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-self-test-report",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--cuda-isolation-test-report",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--cuda-isolation-model-dir",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = AppPaths.from_root(args.data_dir) if args.data_dir else AppPaths.default()
    logger: logging.Logger | None = None
    try:
        paths.ensure()
        logger = configure_logging(paths, "INFO", console=not getattr(sys, "frozen", False))
        settings = SettingsStore(paths).load_or_create()
        if settings.log_level != "INFO":
            logger = configure_logging(
                paths,
                settings.log_level,
                console=not getattr(sys, "frozen", False),
            )
    except (OSError, SettingsError, DatabaseError) as exc:
        if logger is not None:
            logger.error("GUI initialization failed: %s", type(exc).__name__)
            close_logging(logger)
        print(f"GUI 初始化失敗：{exc}", file=sys.stderr)
        return 2

    logger.info(
        "GUI application started: version=%s frozen=%s data_root=%s",
        __version__,
        bool(getattr(sys, "frozen", False)),
        paths.root,
    )
    try:
        if args.cuda_isolation_test_report is not None:
            if args.cuda_isolation_model_dir is None:
                logger.error("CUDA isolation test requires a model directory")
                return 4
            from app.diagnostics.cuda_isolation import write_cuda_isolation_test

            passed = write_cuda_isolation_test(
                args.cuda_isolation_test_report,
                args.cuda_isolation_model_dir,
            )
            logger.info("Packaged CUDA isolation test completed: passed=%s", passed)
            return 0 if passed else 4

        if args.runtime_self_test_report is not None:
            from app.diagnostics.runtime_self_test import write_runtime_self_test

            passed = write_runtime_self_test(args.runtime_self_test_report)
            logger.info("Packaged runtime self-test completed: passed=%s", passed)
            return 0 if passed else 3

        application = QApplication.instance() or QApplication(["video-chapter-tool-gui"])
        application.setApplicationName("直播影片摘要工具")
        application.setOrganizationName("VideoChapterTool")
        icon_path = gui_asset_path("app_icon.svg")
        if icon_path.is_file():
            application.setWindowIcon(QIcon(str(icon_path)))

        previous_exception_hook = sys.excepthook

        def handle_unhandled_exception(
            exception_type: type[BaseException],
            exception: BaseException,
            exception_traceback: object,
        ) -> None:
            frames = traceback.extract_tb(exception_traceback)  # type: ignore[arg-type]
            stack = " > ".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in frames[-8:]
            )
            logger.critical(
                "Unhandled GUI exception: type=%s stack=%s",
                exception_type.__name__,
                stack or "unavailable",
            )
            QMessageBox.critical(
                None,
                "未預期的程式錯誤",
                "程式發生未預期錯誤，請保留日誌與診斷資料。\n"
                f"日誌：{paths.application_log_file}",
            )

        sys.excepthook = handle_unhandled_exception
        try:
            window = MainWindow(paths, auto_refresh=not args.smoke_test)
            window.show()
            if args.smoke_test:
                QTimer.singleShot(100, application.quit)
            return int(application.exec())
        finally:
            sys.excepthook = previous_exception_hook
    finally:
        logger.info("GUI application stopped")
        close_logging(logger)


def entrypoint() -> None:
    raise SystemExit(main())
