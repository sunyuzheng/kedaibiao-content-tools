from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from youtube_to_podcast.config import load_config, write_starter_config
from youtube_to_podcast.hashing import sha256_file, sha256_text, verify_plan
from youtube_to_podcast.planner import build_plan
from youtube_to_podcast.youtube import ListedVideo


NEWEST = "AAAAAAAAAAA"
MIDDLE = "BBBBBBBBBBB"
BASELINE = "CCCCCCCCCCC"
HISTORICAL = "DDDDDDDDDDD"


def episode(video_id: str, *, episode_id: str, status: str = "published") -> dict:
    return {
        "id": episode_id,
        "attributes": {
            "status": status,
            "title": video_id,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "media_url": "https://example.invalid/audio.mp3",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    }


class FakeClient:
    def __init__(self, episodes: list[dict]) -> None:
        self.episodes = episodes

    def episodes_by_video_id(self, show_id: str):
        mapping: dict[str, list[dict]] = {}
        for item in self.episodes:
            video_id = item["attributes"]["video_url"].split("v=", 1)[1]
            mapping.setdefault(video_id, []).append(item)
        return self.episodes, mapping


def metadata(video_id: str) -> dict:
    return {
        "video_id": video_id,
        "title": f"Title {video_id}",
        "description": f"Description {video_id}",
        "upload_date": "20260102",
        "availability": "public",
        "is_live": False,
        "was_live": False,
        "live_status": "not_live",
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "image_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config_path = self.root / "youtube-to-podcast.toml"
        write_starter_config(
            config_path,
            channel_url="https://www.youtube.com/@example/videos",
            show_id="12345",
        )
        self.config = load_config(config_path)
        self.listed = [
            ListedVideo(NEWEST, "Newest", 1),
            ListedVideo(MIDDLE, "Middle", 2),
            ListedVideo(BASELINE, "Baseline", 3),
            ListedVideo(HISTORICAL, "Historical", 4),
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, video_id: str, output_dir: Path) -> dict:
        output_dir.mkdir(parents=True, exist_ok=True)
        audio = output_dir / f"{video_id}.mp3"
        transcript = output_dir / f"{video_id}.en.srt"
        audio.write_bytes(f"audio-{video_id}".encode())
        transcript.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            encoding="utf-8",
        )
        return {
            "audio_path": audio,
            "audio_sha256": sha256_file(audio),
            "audio_bytes": audio.stat().st_size,
            "transcript_path": transcript,
            "transcript_sha256": sha256_text("Hello"),
            "transcript_chars": 5,
        }

    def test_incremental_orders_new_items_oldest_first(self) -> None:
        plan = build_plan(
            self.config,
            FakeClient([episode(BASELINE, episode_id="10")]),
            self.listed,
            fetch_metadata=metadata,
            prepare_media=self.prepare,
        )
        self.assertEqual(
            [item["video_id"] for item in plan["actions"]],
            [MIDDLE, NEWEST],
        )
        self.assertTrue(
            all(item["action"] == "create_draft" for item in plan["actions"])
        )
        self.assertEqual(plan["summary"]["historical_gap_count"], 1)
        self.assertIn(
            "historical_gap_requires_backfill_mode",
            plan["blocked"][0]["reasons"],
        )
        verify_plan(plan, plan["approval_hash"])

    def test_empty_or_unlinked_show_requires_backfill(self) -> None:
        plan = build_plan(
            self.config,
            FakeClient([]),
            self.listed,
            fetch_metadata=metadata,
            prepare_media=self.prepare,
        )
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["summary"]["historical_gap_count"], 4)
        self.assertEqual(
            plan["blocked"][0]["reasons"],
            ["empty_or_unlinked_show_requires_backfill_mode"],
        )

    def test_duplicate_remote_episode_is_blocked(self) -> None:
        plan = build_plan(
            self.config,
            FakeClient(
                [
                    episode(BASELINE, episode_id="10"),
                    episode(NEWEST, episode_id="11"),
                    episode(NEWEST, episode_id="12"),
                ]
            ),
            self.listed,
            fetch_metadata=metadata,
            prepare_media=self.prepare,
        )
        blocked = {
            item["video_id"]: item["reasons"] for item in plan["blocked"]
        }
        self.assertEqual(blocked[NEWEST], ["multiple_remote_episodes"])

    def test_existing_draft_is_never_reused_automatically(self) -> None:
        plan = build_plan(
            self.config,
            FakeClient(
                [
                    episode(BASELINE, episode_id="10"),
                    episode(NEWEST, episode_id="11", status="draft"),
                ]
            ),
            self.listed,
            fetch_metadata=metadata,
            prepare_media=self.prepare,
        )
        blocked = {
            item["video_id"]: item["reasons"] for item in plan["blocked"]
        }
        self.assertEqual(
            blocked[NEWEST],
            ["existing_draft_requires_manual_review"],
        )

    def test_blocked_candidate_does_not_consume_action_quota(self) -> None:
        listed = [
            ListedVideo("AAAAAAAAAAA", "Newest 1", 1),
            ListedVideo("BBBBBBBBBBB", "Newest 2", 2),
            ListedVideo("DDDDDDDDDDD", "Newest 3", 3),
            ListedVideo("EEEEEEEEEEE", "Newest 4", 4),
            ListedVideo("FFFFFFFFFFF", "Blocked oldest new", 5),
            ListedVideo(BASELINE, "Baseline", 6),
        ]

        def fetch(video_id: str) -> dict:
            result = metadata(video_id)
            if video_id == "FFFFFFFFFFF":
                result["title"] = ""
            return result

        plan = build_plan(
            self.config,
            FakeClient([episode(BASELINE, episode_id="10")]),
            listed,
            fetch_metadata=fetch,
            prepare_media=self.prepare,
        )
        self.assertEqual(len(plan["actions"]), 3)
        self.assertEqual(
            [item["video_id"] for item in plan["actions"]],
            ["EEEEEEEEEEE", "DDDDDDDDDDD", "BBBBBBBBBBB"],
        )
        self.assertEqual(plan["summary"]["examined_candidate_count"], 4)
        self.assertEqual(plan["summary"]["deferred_candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
