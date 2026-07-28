from __future__ import annotations

import unittest

from youtube_to_podcast.hashing import PlanError, seal_plan, verify_plan


def sample_plan() -> dict:
    return seal_plan(
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
                    "audio_sha256": "abc",
                }
            ],
            "blocked": [],
            "summary": {"action_count": 1},
        }
    )


class HashingTests(unittest.TestCase):
    def test_exact_approval_is_accepted(self) -> None:
        plan = sample_plan()
        verify_plan(plan, plan["approval_hash"])

    def test_action_tampering_invalidates_plan(self) -> None:
        plan = sample_plan()
        plan["actions"][0]["video_id"] = "BBBBBBBBBBB"
        with self.assertRaisesRegex(PlanError, "Plan hash mismatch"):
            verify_plan(plan, plan["approval_hash"])

    def test_wrong_approval_hash_fails(self) -> None:
        plan = sample_plan()
        with self.assertRaisesRegex(PlanError, "does not match"):
            verify_plan(plan, "0" * 64)


if __name__ == "__main__":
    unittest.main()
