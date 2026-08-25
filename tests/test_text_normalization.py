from __future__ import annotations

import unittest

from app.text_normalization import (
    TaiwanTraditionalNormalizer,
    build_text_normalizer,
)


class TextNormalizationTests(unittest.TestCase):
    def test_s2twp_converts_characters_and_taiwan_phrases(self) -> None:
        normalize = TaiwanTraditionalNormalizer()

        self.assertEqual(normalize("汉字和软件"), "漢字和軟體")

    def test_only_explicit_chinese_uses_converter(self) -> None:
        self.assertIsNone(
            build_text_normalizer(
                language=None,
                traditional_chinese_output=True,
            )
        )
        self.assertIsNone(
            build_text_normalizer(
                language="en",
                traditional_chinese_output=True,
            )
        )
