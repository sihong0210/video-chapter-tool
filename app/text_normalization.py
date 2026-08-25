from __future__ import annotations

from typing import Protocol


class TextNormalizer(Protocol):
    def __call__(self, text: str) -> str: ...


class TextNormalizationError(RuntimeError):
    """Raised when the configured Chinese text converter is unavailable."""


class TaiwanTraditionalNormalizer:
    """Convert Chinese text to Taiwan Traditional with phrase conversion."""

    def __init__(self) -> None:
        try:
            from opencc_pyo3 import OpenCC, OpenccConfig

            self._converter = OpenCC(OpenccConfig.S2TWP)
        except (ImportError, RuntimeError, ValueError) as exc:
            raise TextNormalizationError(
                "無法載入臺灣繁體轉換器，請重新安裝 opencc-pyo3。"
            ) from exc

    def __call__(self, text: str) -> str:
        try:
            return str(self._converter.convert(text))
        except (RuntimeError, ValueError) as exc:
            raise TextNormalizationError("逐字稿繁體轉換失敗。") from exc


def build_text_normalizer(
    *, language: str | None, traditional_chinese_output: bool
) -> TextNormalizer | None:
    if language != "zh" or not traditional_chinese_output:
        return None
    return TaiwanTraditionalNormalizer()
