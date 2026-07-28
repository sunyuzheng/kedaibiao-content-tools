from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from youtube_to_podcast.config import load_config, write_starter_config
from youtube_to_podcast.executor import apply_plan
from youtube_to_podcast.hashing import (
    PlanError,
    seal_plan,
    sha256_file,
    sha256_text,
)


class FailIfCalledClient:
    def episodes_by_video_id(self, show_id: str):
        raise AssertionError("remote state must not be read after approval failure")


class FakePublishingClient:
    def __init__(self) -> None:
        self.episode: dict | None = None
        self.uploaded = False

    def episodes_by_video_id(self, show_id: str):
        return [], {}

    def authorize_upload(self, filename: str):
        return {
            "upload_url": "https://upload.invalid/signed",
            "content_type": "audio/mpeg",
            "audio_url": "https://cdn.invalid/audio.mp3",
        }

    def upload_audio(self, upload_url: str, content_type: str, path: Path) -> None:
        self.uploaded = True

    def create_episode(self, desired: dict):
        attrs = dict(desired)
        attrs["status"] = "draft"
        attrs["number"] = 1
        attrs["media_url"] = attrs.pop("audio_url")
        self.episode = {"id": "99", "attributes": attrs}
        return self.episode

    def update_episode(self, episode_id: str, desired: dict):
        assert self.episode is not None
        self.episode["attributes"].update(desired)
        return self.episode

    def get_episode(self, episode_id: str):
        assert self.episode is not None
        return self.episode

    def publish_episode(self, episode_id: str, published_at: str):
        assert self.episode is not None
        self.episode["attributes"]["status"] = "published"
        self.episode["attributes"]["published_at"] = published_at
        return self.episode


class ExecutorTests(unittest.TestCase):
    def test_wrong_approval_fails_before_remote_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube-to-podcast.toml"
            write_starter_config(
                path,
                channel_url="https://www.youtube.com/@example/videos",
                show_id="12345",
            )
            config = load_config(path)
            plan = seal_plan(
                {
                    "schema_version": 1,
                    "kind": "youtube_to_podcast_plan",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "show_id": config.show_id,
                    "channel_url": config.channel_url,
                    "mode": config.policy.mode,
                    "publication": config.policy.publication,
                    "actions": [],
                    "blocked": [],
                    "summary": {"action_count": 0},
                }
            )
            with self.assertRaises(PlanError):
                apply_plan(
                    config,
                    FailIfCalledClient(),
                    plan,
                    supplied_approval_hash="0" * 64,
                )

    def test_approved_publish_uploads_transcript_and_preserves_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "youtube-to-podcast.toml"
            write_starter_config(
                path,
                channel_url="https://www.youtube.com/@example/videos",
                show_id="12345",
            )
            text = path.read_text(encoding="utf-8").replace(
                'publication = "draft"',
                'publication = "publish"',
            )
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
            media = root / "media"
            media.mkdir()
            audio = media / "AAAAAAAAAAA.mp3"
            transcript = media / "AAAAAAAAAAA.en.srt"
            audio.write_bytes(b"audio")
            transcript.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            description = "Episode description"
            plan = seal_plan(
                {
                    "schema_version": 1,
                    "kind": "youtube_to_podcast_plan",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "show_id": config.show_id,
                    "channel_url": config.channel_url,
                    "mode": config.policy.mode,
                    "publication": config.policy.publication,
                    "actions": [
                        {
                            "action": "create_and_publish",
                            "video_id": "AAAAAAAAAAA",
                            "title": "Episode title",
                            "description": description,
                            "description_sha256": sha256_text(description),
                            "video_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                            "image_url": "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg",
                            "published_at": "2025-12-31T00:00:00Z",
                            "audio_path": "media/AAAAAAAAAAA.mp3",
                            "audio_sha256": sha256_file(audio),
                            "transcript_path": "media/AAAAAAAAAAA.en.srt",
                            "transcript_sha256": sha256_text("Hello"),
                            "remote_precondition": None,
                        }
                    ],
                    "blocked": [],
                    "summary": {"action_count": 1},
                }
            )
            client = FakePublishingClient()
            with redirect_stdout(StringIO()):
                result = apply_plan(
                    config,
                    client,
                    plan,
                    supplied_approval_hash=plan["approval_hash"],
                )
            self.assertTrue(client.uploaded)
            self.assertEqual(result["created"], 1)
            self.assertEqual(result["published"], 1)
            assert client.episode is not None
            attrs = client.episode["attributes"]
            self.assertEqual(attrs["transcript_text"], "Hello")
            self.assertEqual(attrs["published_at"], "2025-12-31T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
