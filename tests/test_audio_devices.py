from __future__ import annotations

import unittest

from app.audio.devices import (
    LoopbackDevice,
    LoopbackDeviceNotFound,
    _select_loopback_info,
)


class _FakeAudioManager:
    def __init__(self) -> None:
        self.devices = [
            {
                "index": 16,
                "name": "Speakers [Loopback]",
                "hostApi": 2,
                "maxInputChannels": 2,
                "defaultSampleRate": 48000.0,
                "isLoopbackDevice": True,
            },
            {
                "index": 17,
                "name": "Headphones [Loopback]",
                "hostApi": 2,
                "maxInputChannels": 2,
                "defaultSampleRate": 44100.0,
                "isLoopbackDevice": True,
            },
        ]

    def get_loopback_device_info_generator(self):
        yield from self.devices

    def get_default_wasapi_loopback(self):
        return self.devices[1]


class LoopbackDeviceTests(unittest.TestCase):
    def test_default_loopback_is_selected(self) -> None:
        selected = _select_loopback_info(_FakeAudioManager(), None)
        self.assertEqual(selected["index"], 17)

    def test_explicit_loopback_is_selected(self) -> None:
        selected = _select_loopback_info(_FakeAudioManager(), 16)
        self.assertEqual(selected["index"], 16)

    def test_unknown_device_index_is_rejected(self) -> None:
        with self.assertRaises(LoopbackDeviceNotFound):
            _select_loopback_info(_FakeAudioManager(), 999)

    def test_mapping_is_normalized(self) -> None:
        info = _FakeAudioManager().devices[0]
        device = LoopbackDevice.from_mapping(info, default_index=16)

        self.assertEqual(device.index, 16)
        self.assertEqual(device.channels, 2)
        self.assertEqual(device.sample_rate, 48000)
        self.assertTrue(device.is_default)


if __name__ == "__main__":
    unittest.main()

