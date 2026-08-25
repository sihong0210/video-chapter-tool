from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from app.audio.backend import AudioBackendUnavailable, load_pyaudio_backend


class LoopbackDeviceNotFound(RuntimeError):
    """Raised when no matching WASAPI loopback device exists."""


@dataclass(frozen=True, slots=True)
class LoopbackDevice:
    index: int
    name: str
    channels: int
    sample_rate: int
    is_default: bool
    host_api_index: int

    @classmethod
    def from_mapping(
        cls,
        info: Mapping[str, Any],
        *,
        default_index: int | None,
    ) -> "LoopbackDevice":
        return cls(
            index=int(info["index"]),
            name=str(info["name"]),
            channels=max(1, int(info["maxInputChannels"])),
            sample_rate=max(1, int(float(info["defaultSampleRate"]))),
            is_default=int(info["index"]) == default_index,
            host_api_index=int(info["hostApi"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_loopback_info(audio_manager: Any) -> Mapping[str, Any] | None:
    try:
        info = audio_manager.get_default_wasapi_loopback()
    except (AttributeError, OSError):
        return None
    return info if isinstance(info, Mapping) else None


def _loopback_info_list(audio_manager: Any) -> list[Mapping[str, Any]]:
    try:
        raw_devices: Iterable[Mapping[str, Any]] = (
            audio_manager.get_loopback_device_info_generator()
        )
        return [device for device in raw_devices if device.get("isLoopbackDevice")]
    except (AttributeError, OSError) as exc:
        raise LoopbackDeviceNotFound(
            "Windows WASAPI Loopback 裝置列舉失敗。"
        ) from exc


def _select_loopback_info(
    audio_manager: Any,
    requested_index: int | None,
) -> Mapping[str, Any]:
    devices = _loopback_info_list(audio_manager)
    if not devices:
        raise LoopbackDeviceNotFound("找不到 Windows WASAPI Loopback 裝置。")

    if requested_index is not None:
        for device in devices:
            if int(device["index"]) == requested_index:
                return device
        raise LoopbackDeviceNotFound(
            f"找不到索引 {requested_index} 的 WASAPI Loopback 裝置。"
        )

    default_info = _default_loopback_info(audio_manager)
    if default_info is not None:
        default_index = int(default_info["index"])
        for device in devices:
            if int(device["index"]) == default_index:
                return device

    return devices[0]


def get_loopback_devices() -> list[LoopbackDevice]:
    pyaudio = load_pyaudio_backend()
    try:
        with pyaudio.PyAudio() as audio_manager:
            devices = _loopback_info_list(audio_manager)
            default_info = _default_loopback_info(audio_manager)
            default_index = (
                int(default_info["index"])
                if default_info is not None
                else None
            )
            return [
                LoopbackDevice.from_mapping(
                    device,
                    default_index=default_index,
                )
                for device in devices
            ]
    except AudioBackendUnavailable:
        raise
    except OSError as exc:
        raise LoopbackDeviceNotFound(
            "無法初始化 Windows WASAPI 音訊系統。"
        ) from exc

