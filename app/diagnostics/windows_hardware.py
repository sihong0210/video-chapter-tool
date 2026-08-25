from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
from ctypes import wintypes
from typing import Any


POWERSHELL_TIMEOUT_SECONDS = 10
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
MAXPNAMELEN = 32


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _DisplayDeviceW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class _WaveOutCapsW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


def get_memory_info() -> tuple[dict[str, Any], list[str]]:
    if os.name != "nt":
        return {}, ["目前不是 Windows，略過 GlobalMemoryStatusEx。"]

    memory_status = _MemoryStatusEx()
    memory_status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
            ctypes.byref(memory_status)
        )
    except (AttributeError, OSError) as exc:
        return {}, [f"無法讀取實體記憶體資訊：{type(exc).__name__}"]

    if not succeeded:
        return {}, ["GlobalMemoryStatusEx 回報失敗。"]

    gibibyte = 1024**3
    return (
        {
            "total_gib": round(memory_status.ullTotalPhys / gibibyte, 2),
            "available_gib": round(memory_status.ullAvailPhys / gibibyte, 2),
            "load_percent": int(memory_status.dwMemoryLoad),
        },
        [],
    )


def _powershell_executable() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _run_cim_query(class_name: str, properties: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[str]]:
    if os.name != "nt":
        return [], [f"目前不是 Windows，略過 {class_name}。"]

    powershell = _powershell_executable()
    if powershell is None:
        return [], [f"找不到 PowerShell，無法查詢 {class_name}。"]

    property_list = ",".join(properties)
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"Get-CimInstance -ClassName {class_name} | "
        f"Select-Object {property_list} | ConvertTo-Json -Compress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=POWERSHELL_TIMEOUT_SECONDS,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [f"查詢 {class_name} 失敗：{type(exc).__name__}"]

    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"PowerShell exit code {completed.returncode}"
        return [], [f"查詢 {class_name} 失敗：{reason}"]

    output = completed.stdout.strip()
    if not output:
        return [], []

    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        return [], [f"{class_name} 回傳無法解析的 JSON。"]

    if isinstance(decoded, dict):
        decoded = [decoded]
    if not isinstance(decoded, list):
        return [], [f"{class_name} 回傳非預期資料格式。"]

    devices = [item for item in decoded if isinstance(item, dict)]
    return devices, []


def _get_display_devices_win32() -> tuple[list[dict[str, Any]], list[str]]:
    if os.name != "nt":
        return [], ["目前不是 Windows，略過 EnumDisplayDevices。"]

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_display_devices = user32.EnumDisplayDevicesW
        enum_display_devices.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_DisplayDeviceW),
            wintypes.DWORD,
        ]
        enum_display_devices.restype = wintypes.BOOL
    except (AttributeError, OSError) as exc:
        return [], [f"無法載入 EnumDisplayDevices：{type(exc).__name__}"]

    devices: list[dict[str, Any]] = []
    device_index = 0
    while True:
        device = _DisplayDeviceW()
        device.cb = ctypes.sizeof(_DisplayDeviceW)
        if not enum_display_devices(None, device_index, ctypes.byref(device), 0):
            break
        device_index += 1

        name = device.DeviceString.strip()
        if not name:
            continue
        state = int(device.StateFlags)
        status_parts = []
        if state & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            status_parts.append("Attached")
        if state & DISPLAY_DEVICE_PRIMARY_DEVICE:
            status_parts.append("Primary")
        devices.append(
            {
                "Name": name,
                "Status": ", ".join(status_parts) if status_parts else "Detected",
                "DriverVersion": None,
                "PNPDeviceID": device.DeviceID or None,
            }
        )

    return devices, []


def _get_wave_out_devices_win32() -> tuple[list[dict[str, Any]], list[str]]:
    if os.name != "nt":
        return [], ["目前不是 Windows，略過 waveOutGetDevCaps。"]

    try:
        winmm = ctypes.WinDLL("winmm", use_last_error=True)
        get_device_count = winmm.waveOutGetNumDevs
        get_device_count.argtypes = []
        get_device_count.restype = wintypes.UINT
        get_device_caps = winmm.waveOutGetDevCapsW
        get_device_caps.argtypes = [
            ctypes.c_size_t,
            ctypes.POINTER(_WaveOutCapsW),
            wintypes.UINT,
        ]
        get_device_caps.restype = wintypes.UINT
    except (AttributeError, OSError) as exc:
        return [], [f"無法載入 Windows Multimedia API：{type(exc).__name__}"]

    devices: list[dict[str, Any]] = []
    for device_index in range(int(get_device_count())):
        capabilities = _WaveOutCapsW()
        result = get_device_caps(
            device_index,
            ctypes.byref(capabilities),
            ctypes.sizeof(_WaveOutCapsW),
        )
        if result != 0:
            continue
        devices.append(
            {
                "Name": capabilities.szPname.strip() or f"WaveOut device {device_index}",
                "Status": "Detected",
                "Manufacturer": f"MMID {capabilities.wMid}",
                "PNPDeviceID": None,
            }
        )

    return devices, []


def get_video_controllers() -> tuple[list[dict[str, Any]], list[str]]:
    devices, cim_warnings = _run_cim_query(
        "Win32_VideoController",
        ("Name", "Status", "DriverVersion", "PNPDeviceID"),
    )
    if devices:
        return devices, cim_warnings

    fallback_devices, fallback_warnings = _get_display_devices_win32()
    if fallback_devices:
        return fallback_devices, ["CIM 顯示卡查詢不可用，已改用 Win32 API。"]
    return [], cim_warnings + fallback_warnings


def get_audio_controllers() -> tuple[list[dict[str, Any]], list[str]]:
    devices, cim_warnings = _run_cim_query(
        "Win32_SoundDevice",
        ("Name", "Status", "Manufacturer", "PNPDeviceID"),
    )
    if devices:
        return devices, cim_warnings

    fallback_devices, fallback_warnings = _get_wave_out_devices_win32()
    if fallback_devices:
        return fallback_devices, ["CIM 音訊查詢不可用，已改用 Win32 Multimedia API。"]
    return [], cim_warnings + fallback_warnings


def classify_gpu_vendor(name: str) -> str:
    normalized = name.casefold()
    if "nvidia" in normalized:
        return "nvidia"
    if "amd" in normalized or "radeon" in normalized or "advanced micro devices" in normalized:
        return "amd"
    if "intel" in normalized:
        return "intel"
    return "unknown"
