from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Protocol

from app.infrastructure.logging_config import redact_sensitive_text


OPENAI_SECRET = "openai_api_key"
HUGGINGFACE_SECRET = "huggingface_token"

_SECRET_DEFINITIONS = {
    OPENAI_SECRET: (
        "VideoChapterTool/OpenAI",
        "OPENAI_API_KEY",
    ),
    HUGGINGFACE_SECRET: (
        "VideoChapterTool/HuggingFace",
        "HF_TOKEN",
    ),
}


class CredentialError(RuntimeError):
    """Raised when a credential cannot be safely read or changed."""


class CredentialBackend(Protocol):
    def read(self, target: str) -> str | None: ...

    def write(self, target: str, value: str) -> None: ...

    def delete(self, target: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class SecretState:
    configured: bool
    source: str


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", ctypes.c_ulong),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_ulong),
        ("AttributeCount", ctypes.c_ulong),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialBackend:
    """Store per-user generic secrets in Windows Credential Manager."""

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager 只支援 Windows。")
        try:
            self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            self._cred_write = self._advapi32.CredWriteW
            self._cred_write.argtypes = [ctypes.POINTER(_CredentialW), ctypes.c_ulong]
            self._cred_write.restype = ctypes.c_int
            self._cred_read = self._advapi32.CredReadW
            self._cred_read.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.POINTER(_CredentialW)),
            ]
            self._cred_read.restype = ctypes.c_int
            self._cred_delete = self._advapi32.CredDeleteW
            self._cred_delete.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            self._cred_delete.restype = ctypes.c_int
            self._cred_free = self._advapi32.CredFree
            self._cred_free.argtypes = [ctypes.c_void_p]
        except (AttributeError, OSError) as exc:
            raise CredentialError("無法載入 Windows Credential Manager。") from exc

    def read(self, target: str) -> str | None:
        pointer = ctypes.POINTER(_CredentialW)()
        if not self._cred_read(
            target,
            self._CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error_code = ctypes.get_last_error()
            if error_code == self._ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"無法讀取 Windows 認證資料（{error_code}）。")
        try:
            credential = pointer.contents
            blob = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return blob.decode("utf-16-le") if blob else ""
        except (UnicodeError, ValueError) as exc:
            raise CredentialError("Windows 認證資料格式無法解析。") from exc
        finally:
            self._cred_free(pointer)

    def write(self, target: str, value: str) -> None:
        value = value.strip()
        if not value:
            raise CredentialError("金鑰不可為空。")
        blob = bytearray(value.encode("utf-16-le"))
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer(blob)
        credential = _CredentialW()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            buffer,
            ctypes.POINTER(ctypes.c_ubyte),
        )
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "VideoChapterTool"
        try:
            if not self._cred_write(ctypes.byref(credential), 0):
                error_code = ctypes.get_last_error()
                raise CredentialError(
                    f"無法寫入 Windows 認證資料（{error_code}）。"
                )
        finally:
            for index in range(len(blob)):
                blob[index] = 0

    def delete(self, target: str) -> bool:
        if self._cred_delete(target, self._CRED_TYPE_GENERIC, 0):
            return True
        error_code = ctypes.get_last_error()
        if error_code == self._ERROR_NOT_FOUND:
            return False
        raise CredentialError(f"無法刪除 Windows 認證資料（{error_code}）。")


class MemoryCredentialBackend:
    """Test backend with the same behavior and no operating-system writes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, target: str) -> str | None:
        return self.values.get(target)

    def write(self, target: str, value: str) -> None:
        self.values[target] = value

    def delete(self, target: str) -> bool:
        return self.values.pop(target, None) is not None


class SecretStore:
    def __init__(self, backend: CredentialBackend | None = None) -> None:
        self.backend = backend or WindowsCredentialBackend()

    def _definition(self, secret_name: str) -> tuple[str, str]:
        try:
            return _SECRET_DEFINITIONS[secret_name]
        except KeyError as exc:
            raise ValueError(f"不支援的金鑰種類：{secret_name}") from exc

    def get(self, secret_name: str) -> str | None:
        target, environment_name = self._definition(secret_name)
        environment_value = os.environ.get(environment_name, "").strip()
        if not environment_value and secret_name == HUGGINGFACE_SECRET:
            environment_value = os.environ.get("HUGGINGFACE_TOKEN", "").strip()
        if environment_value:
            return environment_value
        value = self.backend.read(target)
        return value.strip() if value and value.strip() else None

    def state(self, secret_name: str) -> SecretState:
        _, environment_name = self._definition(secret_name)
        environment_present = bool(os.environ.get(environment_name, "").strip())
        if secret_name == HUGGINGFACE_SECRET:
            environment_present = environment_present or bool(
                os.environ.get("HUGGINGFACE_TOKEN", "").strip()
            )
        if environment_present:
            return SecretState(True, "environment")
        target, _ = self._definition(secret_name)
        return SecretState(bool(self.backend.read(target)), "credential_manager")

    def set(self, secret_name: str, value: str) -> None:
        target, _ = self._definition(secret_name)
        try:
            self.backend.write(target, value.strip())
        except CredentialError:
            raise
        except Exception as exc:
            safe = redact_sensitive_text(str(exc))
            raise CredentialError(f"無法保存金鑰：{safe}") from exc

    def delete(self, secret_name: str) -> bool:
        target, _ = self._definition(secret_name)
        return self.backend.delete(target)
