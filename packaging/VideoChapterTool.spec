# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


ROOT = Path(SPECPATH).parent.resolve()

datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []

datas.append((str(ROOT / "app" / "gui" / "assets" / "app_icon.svg"), "app/gui/assets"))


def add_collection(package: str) -> None:
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)


# These packages are imported only after the user starts a particular workflow,
# so static analysis cannot see them from the GUI entry point.
for dynamic_package in (
    "ctranslate2",
    "faster_whisper",
    "opencc_pyo3",
    "openai",
    "pyaudiowpatch",
    "pyannote.audio",
):
    add_collection(dynamic_package)

# pyannote model configuration names pipeline/model classes by dotted path.
# Keep all pyannote.audio implementation modules available for those imports.
hiddenimports.extend(collect_submodules("pyannote.audio"))
# SciPy 1.17 loads its vendored Array API compatibility namespaces through
# importlib; PyInstaller's current SciPy hook still looks under the old path.
hiddenimports.extend(collect_submodules("scipy._external.array_api_compat"))

# Runtime diagnostics query these distribution names with importlib.metadata.
for distribution in (
    "ctranslate2",
    "faster-whisper",
    "huggingface-hub",
    "lightning",
    "openai",
    "opencc-pyo3",
    "pyannote.audio",
    "PyAudioWPatch",
    "torch",
    "torchaudio",
):
    try:
        datas.extend(copy_metadata(distribution))
    except Exception:
        # Some packages are optional when a CPU-only build environment is used.
        pass

# CTranslate2 4.8 uses CUDA 12 for Whisper.  The build script may point at an
# installed CUDA 12 runtime so its two redistributable BLAS DLLs travel with the
# NVIDIA-capable Alpha package.  Models and application data are never bundled.
cuda12_bin = os.environ.get("VCT_CUDA12_BIN")
if cuda12_bin:
    for dll_name in ("cublas64_12.dll", "cublasLt64_12.dll"):
        dll_path = Path(cuda12_bin) / dll_name
        if dll_path.is_file():
            binaries.append((str(dll_path), "."))


a = Analysis(
    [str(ROOT / "packaging" / "gui_launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The application gives pyannote an in-memory waveform and does not use its
    # optional TorchCodec/FFmpeg file decoder.
    excludes=["torchcodec"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoChapterTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / "packaging" / "windows_version_info.txt"),
    icon=str(ROOT / "app" / "gui" / "assets" / "app_icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VideoChapterTool",
)
