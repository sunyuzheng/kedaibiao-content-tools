from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from youtube_to_podcast.transcript import choose_transcript, timed_text_to_text


class TranscriptTests(unittest.TestCase):
    def test_srt_and_vtt_are_converted_to_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_text(
                """\
1
00:00:00,000 --> 00:00:01,000
<b>Hello &amp; welcome</b>

2
00:00:01,000 --> 00:00:02,000
Hello &amp; welcome

3
00:00:02,000 --> 00:00:03,000
Next line
""",
                encoding="utf-8",
            )
            self.assertEqual(
                timed_text_to_text(path),
                "Hello & welcome\nNext line",
            )

    def test_human_srt_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            human = root / "video.zh.srt"
            auto = root / "video.zh.auto.srt"
            vtt = root / "video.en.vtt"
            for path in (human, auto, vtt):
                path.write_text("x" * 30, encoding="utf-8")
            self.assertEqual(choose_transcript([auto, vtt, human]), human)


if __name__ == "__main__":
    unittest.main()
