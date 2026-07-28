from __future__ import annotations

import unittest

from youtube_to_podcast.transistor import extract_video_id


class TransistorTests(unittest.TestCase):
    def test_extracts_supported_youtube_url_shapes(self) -> None:
        video_id = "AAAAAAAAAAA"
        urls = [
            f"https://www.youtube.com/watch?feature=share&v={video_id}",
            f"https://youtu.be/{video_id}?si=example",
            f"https://www.youtube.com/shorts/{video_id}",
            f"https://www.youtube.com/live/{video_id}",
            f"https://www.youtube.com/embed/{video_id}",
        ]
        self.assertEqual(
            [extract_video_id(url) for url in urls],
            [video_id] * len(urls),
        )

    def test_rejects_non_youtube_or_invalid_id(self) -> None:
        self.assertIsNone(extract_video_id("https://example.com/AAAAAAAAAAA"))
        self.assertIsNone(extract_video_id("https://youtube.com/watch?v=short"))


if __name__ == "__main__":
    unittest.main()
