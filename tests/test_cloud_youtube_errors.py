"""Cloud diagnostics preserve failure categories without exposing service output."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.automation import cloud_podcast as cloud
from tools.podcast.cloud_state import CloudBlocked


VIDEO_ID = "AAAAAAAAAAA"
PRIVATE_FIXTURE = "https://cdn.example.invalid/audio?token=DO_NOT_ECHO_TEST_TOKEN"
CASES = (
    ("Sign in to confirm you're not a bot", "bot_or_sign_in_challenge"),
    ("HTTP Error 403: Forbidden", "http_403_forbidden"),
    ("HTTP Error 429: Too Many Requests", "http_429_rate_limit"),
    ("Join this channel to get access to members-only content", "member_only"),
    ("No supported JavaScript runtime could be found", "javascript_runtime_or_challenge_solver"),
    ("[jsc] Challenge solving failed", "javascript_runtime_or_challenge_solver"),
    ("Requested format is not available. Use --list-formats", "format_unavailable"),
    ("Unable to download webpage: Connection timed out", "network_error"),
    ("Unexpected extractor failure", "unclassified_extraction_error"),
)


def stderr_for(message: str) -> str:
    return f"ERROR: [youtube] {VIDEO_ID}: {message}\nDebug URL: {PRIVATE_FIXTURE}"


class YouTubeDiagnosticTests(unittest.TestCase):
    def test_distinct_fixed_labels_never_copy_service_details(self) -> None:
        for message, label in CASES:
            with self.subTest(label=label, message=message):
                actual = cloud.classify_youtube_error(stderr_for(message))
                self.assertEqual(actual, label)
                self.assertNotIn("https://", actual)
                self.assertNotIn("DO_NOT_ECHO_TEST_TOKEN", actual)

    def test_challenge_failure_takes_precedence_over_secondary_format_error(self) -> None:
        text = "WARNING: [jsc] Challenge solving failed\nERROR: Requested format is not available"
        self.assertEqual(cloud.classify_youtube_error(text), "javascript_runtime_or_challenge_solver")

    def test_signed_url_contents_cannot_select_a_failure_category(self) -> None:
        text = "Unexpected failure: https://cdn.example.invalid/members-only?status=403&token=DO_NOT_ECHO_TEST_TOKEN"
        self.assertEqual(cloud.classify_youtube_error(text), "unclassified_extraction_error")

    def test_probe_reports_category_without_raw_stderr_and_preserves_members(self) -> None:
        for message, label in CASES:
            with self.subTest(label=label):
                result = subprocess.CompletedProcess([], 1, "", stderr_for(message))
                with patch.object(cloud.subprocess, "run", return_value=result):
                    if label == "member_only":
                        self.assertEqual(cloud.verify_probe(VIDEO_ID, cloud.probe(VIDEO_ID)), "excluded_member")
                        continue
                    with self.assertRaises(CloudBlocked) as caught:
                        cloud.probe(VIDEO_ID)
                error = str(caught.exception)
                self.assertIn(f"diagnostic={label}", error)
                self.assertNotIn(PRIVATE_FIXTURE, error)
                self.assertNotIn("DO_NOT_ECHO_TEST_TOKEN", error)
                self.assertNotIn("Debug URL", error)

    def test_failed_audio_download_reports_safe_category(self) -> None:
        info = {"id": VIDEO_ID, "title": "Test episode", "upload_date": "20260930"}
        with tempfile.TemporaryDirectory() as directory, patch.object(cloud, "ROOT", Path(directory)):
            for message, label in (CASES[0], CASES[1], CASES[6]):
                with self.subTest(label=label):
                    result = subprocess.CompletedProcess([], 1, "", stderr_for(message))
                    with patch.object(cloud.subprocess, "run", return_value=result):
                        with self.assertRaises(CloudBlocked) as caught:
                            cloud.download(info)
                    self.assertEqual(str(caught.exception), f"Cloud audio download failed: {VIDEO_ID}; diagnostic={label}")


if __name__ == "__main__":
    unittest.main()
