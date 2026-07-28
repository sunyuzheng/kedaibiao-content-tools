from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from youtube_to_podcast.hashing import seal_plan
from youtube_to_podcast.review import render_review


class ReviewTests(unittest.TestCase):
    def test_review_is_self_contained_and_escapes_youtube_text(self) -> None:
        plan = seal_plan(
            {
                "schema_version": 1,
                "kind": "youtube_to_podcast_plan",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "show_id": "12345",
                "channel_url": "https://www.youtube.com/@example/videos",
                "mode": "incremental",
                "publication": "draft",
                "actions": [
                    {
                        "action": "create_draft",
                        "video_id": "AAAAAAAAAAA",
                        "title": '<script>alert("no")</script>',
                        "published_at": "2026-01-01T00:00:00Z",
                        "transcript_path": None,
                        "audio_bytes": 1_048_576,
                        "warnings": ["missing_transcript"],
                    }
                ],
                "blocked": [],
                "summary": {
                    "action_count": 1,
                    "deferred_candidate_count": 0,
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "review.html"
            render_review(
                plan,
                output,
                apply_command="youtube-to-podcast apply --approval-hash abc",
            )
            text = output.read_text(encoding="utf-8")
        self.assertIn("YouTube to Podcast", text)
        self.assertIn(plan["approval_hash"], text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn('<script>alert("no")</script>', text)
        self.assertNotIn("https://cdn.", text)


if __name__ == "__main__":
    unittest.main()
