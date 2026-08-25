from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.infrastructure import (
    HUGGINGFACE_SECRET,
    MemoryCredentialBackend,
    OPENAI_SECRET,
    SecretStore,
)
from app.infrastructure.power import (
    ES_CONTINUOUS,
    ES_DISPLAY_REQUIRED,
    ES_SYSTEM_REQUIRED,
    RecordingPowerRequest,
    recording_execution_flags,
)


class CredentialTests(unittest.TestCase):
    def test_environment_has_priority_without_being_persisted(self) -> None:
        backend = MemoryCredentialBackend()
        store = SecretStore(backend)
        store.set(OPENAI_SECRET, "stored-key")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-key"}):
            self.assertEqual(store.get(OPENAI_SECRET), "environment-key")
            self.assertEqual(store.state(OPENAI_SECRET).source, "environment")
        self.assertEqual(store.get(OPENAI_SECRET), "stored-key")

    def test_hugging_face_alias_and_delete(self) -> None:
        backend = MemoryCredentialBackend()
        store = SecretStore(backend)
        store.set(HUGGINGFACE_SECRET, "hf-test")
        self.assertTrue(store.state(HUGGINGFACE_SECRET).configured)
        self.assertTrue(store.delete(HUGGINGFACE_SECRET))
        self.assertFalse(store.state(HUGGINGFACE_SECRET).configured)

    def test_power_request_uses_display_flag_and_restores_policy(self) -> None:
        calls: list[int] = []
        request = RecordingPowerRequest(
            keep_display_on=True,
            setter=lambda flags: calls.append(flags) or 1,
        )
        with request:
            self.assertTrue(request.active)
        self.assertEqual(
            calls,
            [ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED, ES_CONTINUOUS],
        )
        self.assertEqual(
            recording_execution_flags(keep_display_on=False),
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED,
        )


if __name__ == "__main__":
    unittest.main()
