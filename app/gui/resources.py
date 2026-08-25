from __future__ import annotations

import sys
from pathlib import Path


def gui_asset_path(filename: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "gui" / "assets" / filename
    return Path(__file__).resolve().parent / "assets" / filename
