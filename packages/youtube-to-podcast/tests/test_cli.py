from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from youtube_to_podcast.cli import command_init, command_update_ytdlp


class CliTests(unittest.TestCase):
    def test_init_gitignores_secret_and_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                config=root / "youtube-to-podcast.toml",
                channel_url="https://www.youtube.com/@example/videos",
                show_id="12345",
                force=False,
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(command_init(args), 0)
            lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertIn(".env", lines)
            self.assertIn(".youtube-to-podcast/", lines)

    @patch("youtube_to_podcast.cli._tool_version", side_effect=["old", "old"])
    @patch("youtube_to_podcast.cli.subprocess.run")
    def test_ytdlp_update_failure_uses_working_current_version(
        self,
        run,
        _version,
    ) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "temporary package index failure"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = command_update_ytdlp(argparse.Namespace(pre=True))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "update_failed_using_current")
        self.assertEqual(payload["version_after"], "old")


if __name__ == "__main__":
    unittest.main()
