from __future__ import annotations

import unittest

from app.transcription.merge import TranscriptMerger
from app.transcription.transcriber import TranscriptSegment


def _segment(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        start=start,
        end=end,
        text=text,
        average_log_probability=-0.2,
        no_speech_probability=0.01,
    )


class TranscriptMergerTests(unittest.TestCase):
    def test_offsets_local_timestamps_and_removes_exact_duplicate(self) -> None:
        merger = TranscriptMerger(overlap_seconds=2.0)

        merger.add_chunk(0.0, (_segment(8.0, 10.0, "今天介紹新品粉底"),))
        added = merger.add_chunk(
            8.0,
            (
                _segment(0.0, 2.0, "今天介紹新品粉底"),
                _segment(2.1, 3.0, "接著比較色號"),
            ),
        )

        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].start, 10.1)
        self.assertEqual([item.text for item in merger.segments], [
            "今天介紹新品粉底",
            "接著比較色號",
        ])

    def test_trims_repeated_text_across_chunk_boundary(self) -> None:
        merger = TranscriptMerger(overlap_seconds=2.0)
        merger.add_chunk(0.0, (_segment(8.0, 10.0, "這款適合乾性肌膚。"),))

        merger.add_chunk(
            8.0,
            (_segment(1.0, 3.0, "乾性肌膚，也適合混合肌膚"),),
        )

        self.assertEqual(len(merger.segments), 2)
        self.assertEqual(merger.segments[1].text, "也適合混合肌膚")
        self.assertEqual(merger.segments[1].start, 10.0)

    def test_trims_english_overlap_despite_punctuation(self) -> None:
        merger = TranscriptMerger(overlap_seconds=2.0)
        merger.add_chunk(
            0.0,
            (_segment(0.0, 5.84, "Americans, ask what your country can do for you."),),
        )

        merger.add_chunk(
            4.0,
            (_segment(0.0, 4.6, "country can do for you ask what you can do."),),
        )

        self.assertEqual(merger.segments[1].text, "ask what you can do.")
        self.assertEqual(merger.segments[1].start, 6.0)

    def test_discards_alternate_recognition_fully_inside_overlap(self) -> None:
        merger = TranscriptMerger(overlap_seconds=2.0)
        merger.add_chunk(0.0, (_segment(22.0, 25.0, "沒有加上三點零"),))

        merger.add_chunk(
            23.0,
            (_segment(0.0, 1.0, "三點零版本"),),
            chunk_end_seconds=48.0,
        )

        self.assertEqual(len(merger.segments), 1)

    def test_clamps_segments_to_chunk_end_and_monotonic_timeline(self) -> None:
        merger = TranscriptMerger(overlap_seconds=2.0)
        merger.add_chunk(0.0, (_segment(23.0, 25.0, "上一段"),))

        merger.add_chunk(
            23.0,
            (_segment(1.0, 26.0, "上一段，接著是新內容"),),
            chunk_end_seconds=48.0,
        )

        self.assertEqual(len(merger.segments), 2)
        self.assertGreaterEqual(merger.segments[1].start, merger.segments[0].end)
        self.assertLessEqual(merger.segments[1].end, 48.0)


if __name__ == "__main__":
    unittest.main()
