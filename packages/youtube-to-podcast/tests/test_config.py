from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from youtube_to_podcast.config import ConfigError, load_config, write_starter_config


class ConfigTests(unittest.TestCase):
    def test_starter_defaults_to_incremental_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube-to-podcast.toml"
            write_starter_config(
                path,
                channel_url="https://www.youtube.com/@example/videos",
                show_id="12345",
            )
            config = load_config(path)
            self.assertEqual(config.policy.mode, "incremental")
            self.assertEqual(config.policy.publication, "draft")
            self.assertEqual(config.policy.max_actions, 3)
            self.assertEqual(config.policy.max_candidates, 12)
            self.assertEqual(
                config.work_dir,
                (Path(directory) / ".youtube-to-podcast").resolve(),
            )

    def test_backfill_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube-to-podcast.toml"
            path.write_text(
                """\
schema_version = 1
[youtube]
channel_url = "https://www.youtube.com/@example/videos"
[transistor]
show_id = "12345"
[policy]
mode = "backfill"
publication = "publish"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "intentionally unsupported"):
                load_config(path)

    def test_work_directory_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube-to-podcast.toml"
            path.write_text(
                """\
schema_version = 1
work_dir = "../outside"
[youtube]
channel_url = "https://www.youtube.com/@example/videos"
[transistor]
show_id = "12345"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must stay inside"):
                load_config(path)

    def test_arbitrary_playlist_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube-to-podcast.toml"
            with self.assertRaisesRegex(ConfigError, "not chronological"):
                write_starter_config(
                    path,
                    channel_url="https://www.youtube.com/playlist?list=example",
                    show_id="12345",
                )


if __name__ == "__main__":
    unittest.main()
