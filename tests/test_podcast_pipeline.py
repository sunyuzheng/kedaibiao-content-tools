from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.automation import update_yt_dlp
from tools.automation.email_notification import build_message, send_report
from tools.check.build_draft_cleanup_plan import build_cleanup_plan
from tools.check.build_podcast_sync_plan import local_payload
from tools.podcast.core import (
    PROJECT_ROOT,
    atomic_write_json,
    choose_transcript_path,
    load_env,
    plan_hash,
    sha256_text,
    timed_text_to_text,
)
from tools.podcast.show_notes import validate_show_notes
from tools.podcast.transistor_client import TransistorClient
from tools.upload.apply_podcast_sync_plan import (
    PlanPreconditionError,
    apply_descriptions,
    load_and_verify_plan,
    read_description,
    read_transcript,
)
from tools.youtube.build_incremental_download_queue import (
    local_member_ids,
    two_column_youtube_ids,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        *,
        headers: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class EmailNotificationTests(unittest.TestCase):
    def test_attention_message_contains_counts_and_approval_boundary(self) -> None:
        message = build_message(
            {
                "status": "attention",
                "publish_count": 2,
                "transcript_count": 3,
                "blocked_count": 4,
                "started_at": "2026-07-27T09:15:00+00:00",
                "finished_at": "2026-07-27T09:20:00+00:00",
                "youtube_snapshot_fresh": True,
                "plan_hash": "abc",
                "plan_path": "logs/podcast_sync/plans/example.json",
            }
        )
        self.assertIn("发布 2 / 字幕 3 / 阻止 4", message["subject"])
        self.assertIn("不会自动发布或修改 Transistor", message["text"])

    def test_send_report_is_idempotent_and_does_not_return_credentials(self) -> None:
        session = FakeSession([FakeResponse(200, {"id": "email-one"})])
        env = {
            "RESEND_API_KEY": "test-secret",
            "RESEND_FROM_EMAIL": "Podcast <podcast@example.com>",
            "PODCAST_SYNC_EMAIL_TO": "owner@example.com",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = send_report(
                {
                    "status": "healthy",
                    "started_at": "2026-07-27T09:15:00+00:00",
                },
                session=session,
                sleep=mock.Mock(),
            )
        self.assertEqual(result, {
            "status": "sent",
            "email_id": "email-one",
            "recipient_count": 1,
        })
        headers = session.calls[0][2]["headers"]
        self.assertEqual(
            headers["Idempotency-Key"],
            "kedaibiao-podcast-sync/2026-07-27T09:15:00+00:00",
        )
        self.assertNotIn("test-secret", json.dumps(result))

    def test_partial_configuration_fails_closed_without_network(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RESEND_API_KEY": "test-secret"},
            clear=True,
        ):
            result = send_report(
                {"status": "healthy", "started_at": "run"},
                session=mock.Mock(),
            )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "incomplete_configuration")
        self.assertIn("RESEND_FROM_EMAIL", result["missing"])


class EnvironmentConfigTests(unittest.TestCase):
    def test_load_env_unquotes_single_and_double_quoted_values(self) -> None:
        keys = (
            "KEDAIBIAO_TEST_DOUBLE_QUOTED",
            "KEDAIBIAO_TEST_SINGLE_QUOTED",
            "KEDAIBIAO_TEST_UNQUOTED",
        )
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                'KEDAIBIAO_TEST_DOUBLE_QUOTED="Sender <sender@example.com>"\n'
                "KEDAIBIAO_TEST_SINGLE_QUOTED='recipient@example.com'\n"
                "KEDAIBIAO_TEST_UNQUOTED=plain-value\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                for key in keys:
                    os.environ.pop(key, None)
                load_env(env_path)
                self.assertEqual(
                    os.environ["KEDAIBIAO_TEST_DOUBLE_QUOTED"],
                    "Sender <sender@example.com>",
                )
                self.assertEqual(
                    os.environ["KEDAIBIAO_TEST_SINGLE_QUOTED"],
                    "recipient@example.com",
                )
                self.assertEqual(
                    os.environ["KEDAIBIAO_TEST_UNQUOTED"],
                    "plain-value",
                )


class TranscriptTests(unittest.TestCase):
    def test_srt_and_vtt_are_cleaned_and_exact_duplicates_collapsed(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            source = Path(directory) / "captions.vtt"
            source.write_text(
                "WEBVTT\n\n"
                "00:00:00.000 --> 00:00:01.000\n"
                "<c>你好 &amp; hello</c>\n\n"
                "00:00:01.000 --> 00:00:02.000\n"
                "<c>你好 &amp; hello</c>\n"
                "第二行\n",
                encoding="utf-8",
            )
            self.assertEqual(timed_text_to_text(source), "你好 & hello\n第二行")

    def test_corrected_transcript_wins_over_qwen_and_generic_srt(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            folder = Path(directory)
            generic = folder / "video.zh.srt"
            corrected = folder / "video.corrected.srt"
            qwen = folder / "video.qwen.srt"
            for path in (generic, corrected, qwen):
                path.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕内容\n", encoding="utf-8")
            chosen = choose_transcript_path(folder, {"transcript_status": "local_corrected"})
            self.assertEqual(chosen, corrected.resolve())

    def test_transcript_source_hash_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            source = Path(directory) / "captions.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n原文\n",
                encoding="utf-8",
            )
            relative = str(source.relative_to(PROJECT_ROOT))
            expected = sha256_text(timed_text_to_text(source))
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n已修改\n",
                encoding="utf-8",
            )
            with self.assertRaises(PlanPreconditionError):
                read_transcript(relative, expected)


class ShowNotesTests(unittest.TestCase):
    def test_valid_show_notes_accepts_long_minutes_and_transistor_tags(self) -> None:
        text = (
            "这是足够长的节目简介。" * 20
            + "\n\n章节\n\n"
            + "00:00 — 开场\n"
            + "64:00 — 深入讨论\n"
            + "100:19 — 结尾\n\n"
            + "{{video | title: '观看视频'}}\n"
            + "{{transcript | title: '阅读文字稿'}}\n"
            + "https://www.superlinear.academy/"
        )

        result = validate_show_notes(text)

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["timestamps"], 3)
        self.assertEqual(result["last_timestamp_seconds"], 100 * 60 + 19)
        self.assertEqual(result["placeholders"], ["video", "transcript"])

    def test_show_notes_rejects_out_of_order_timestamps_and_unknown_tag(self) -> None:
        text = (
            "内容" * 160
            + "\n10:00 — 后面\n05:00 — 前面\n"
            + "{{unknown}}\n"
            + "https://staysuperlinear.com/"
        )

        result = validate_show_notes(text)

        self.assertIn("timestamps_not_strictly_increasing", result["errors"])
        self.assertIn("unsupported_placeholder:unknown", result["errors"])
        self.assertIn(
            "legacy_link:staysuperlinear.com",
            result["warnings"],
        )


class PlanTests(unittest.TestCase):
    def test_modified_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            path = Path(directory) / "plan.json"
            plan = {
                "schema_version": 1,
                "kind": "kedaibiao_podcast_sync_plan",
                "publish_actions": [],
                "transcript_actions": [],
            }
            plan["plan_hash"] = plan_hash(plan)
            atomic_write_json(path, plan)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            loaded["publish_actions"].append({"video_id": "changed"})
            atomic_write_json(path, loaded)
            with self.assertRaises(PlanPreconditionError):
                load_and_verify_plan(path)

    def test_incremental_queue_sources_are_strictly_classified(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            root = Path(directory)
            archive = root / "downloaded.txt"
            archive.write_text(
                "youtube public-one\n"
                "youtube public-two\n"
                "other ignored\n"
                "youtube malformed extra\n",
                encoding="utf-8",
            )
            member_folder = root / "nested" / "member"
            member_folder.mkdir(parents=True)
            (member_folder / "member.info.json").write_text(
                json.dumps({"id": "member-one", "availability": "subscriber_only"}),
                encoding="utf-8",
            )
            (member_folder / "public.info.json").write_text(
                json.dumps({"id": "public-three", "availability": "public"}),
                encoding="utf-8",
            )
            self.assertEqual(
                two_column_youtube_ids(archive), {"public-one", "public-two"}
            )
            self.assertEqual(local_member_ids(root), {"member-one"})

    def test_youtube_description_is_locked_into_plan_when_local_file_is_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            folder = Path(directory)
            (folder / "episode.description").write_text("", encoding="utf-8")
            relative_folder = str(folder.relative_to(PROJECT_ROOT))
            payload, warnings = local_payload(
                {
                    "video_id": "video-one",
                    "folder": relative_folder,
                    "title": "Episode",
                    "upload_date": "20260727",
                    "transcript_status": "missing",
                },
                {"description": "  YouTube description  "},
            )

            self.assertEqual(payload["description_source"], "youtube_snapshot")
            self.assertEqual(payload["description_text"], "YouTube description")
            self.assertEqual(payload["description_path"], None)
            self.assertNotIn("missing_description", warnings)
            self.assertEqual(
                read_description(
                    payload["description_path"],
                    payload["description_sha256"],
                    payload["description_text"],
                ),
                "YouTube description",
            )

    def test_inline_description_hash_change_fails_closed(self) -> None:
        with self.assertRaises(PlanPreconditionError):
            read_description(
                None,
                sha256_text("approved"),
                "changed",
            )

    def test_podcast_description_sidecar_wins_over_youtube_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            folder = Path(directory)
            (folder / "episode.description").write_text(
                "Copied YouTube description",
                encoding="utf-8",
            )
            sidecar = folder / "episode.podcast-description.txt"
            sidecar.write_text("Podcast-first show notes", encoding="utf-8")
            relative_folder = str(folder.relative_to(PROJECT_ROOT))

            payload, warnings = local_payload(
                {
                    "video_id": "video-one",
                    "folder": relative_folder,
                    "title": "Episode",
                    "upload_date": "20260727",
                    "transcript_status": "missing",
                },
                {"description": "YouTube snapshot"},
            )

            self.assertEqual(payload["description_source"], "podcast_sidecar")
            self.assertEqual(payload["description_path"], str(sidecar.relative_to(PROJECT_ROOT)))
            self.assertEqual(
                read_description(
                    payload["description_path"],
                    payload["description_sha256"],
                ),
                "Podcast-first show notes",
            )
            self.assertNotIn("missing_description", warnings)

    def test_description_apply_updates_exact_episode_and_reads_back(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            source = Path(directory) / "episode.podcast-description.txt"
            source.write_text("Approved show notes", encoding="utf-8")
            relative_source = str(source.relative_to(PROJECT_ROOT))

            class FakeDescriptionClient:
                def __init__(self) -> None:
                    self.update_calls: list[tuple[str, dict]] = []
                    self.episode = {
                        "id": "episode-1",
                        "attributes": {
                            "status": "published",
                            "video_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                            "description": "Old show notes",
                        },
                    }

                def get_episode(self, episode_id: str) -> dict:
                    self.assert_episode_id(episode_id)
                    return self.episode

                def update_episode(self, episode_id: str, values: dict) -> dict:
                    self.assert_episode_id(episode_id)
                    self.update_calls.append((episode_id, values))
                    self.episode["attributes"].update(values)
                    return self.episode

                @staticmethod
                def assert_episode_id(episode_id: str) -> None:
                    if episode_id != "episode-1":
                        raise AssertionError(episode_id)

            plan = {
                "description_actions": [
                    {
                        "episode_id": "episode-1",
                        "episode_status": "published",
                        "video_id": "AAAAAAAAAAA",
                        "description_path": relative_source,
                        "description_sha256": sha256_text("Approved show notes"),
                        "remote_description_sha256": sha256_text("Old show notes"),
                    }
                ]
            }
            client = FakeDescriptionClient()
            ledger = mock.Mock()

            updated, skipped = apply_descriptions(client, plan, ledger)

            self.assertEqual((updated, skipped), (1, 0))
            self.assertEqual(
                client.update_calls,
                [("episode-1", {"description": "Approved show notes"})],
            )
            ledger.write.assert_called_with(
                "description_updated",
                episode_id="episode-1",
                video_id="AAAAAAAAAAA",
                description_sha256=sha256_text("Approved show notes"),
                description_chars=len("Approved show notes"),
                verification="exact_text_hash",
                dynamic_tags_verified=False,
            )

    def test_description_apply_rejects_unexpanded_dynamic_tags(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "logs") as directory:
            source = Path(directory) / "episode.podcast-description.txt"
            description = "Approved {{video | title: 'Watch'}}"
            source.write_text(description, encoding="utf-8")
            relative_source = str(source.relative_to(PROJECT_ROOT))

            class FakeDescriptionClient:
                def __init__(self) -> None:
                    self.episode = {
                        "id": "episode-1",
                        "attributes": {
                            "status": "published",
                            "video_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                            "description": "Old show notes",
                            "formatted_description": "Old show notes",
                        },
                    }

                def get_episode(self, episode_id: str) -> dict:
                    if episode_id != "episode-1":
                        raise AssertionError(episode_id)
                    return self.episode

                def update_episode(self, episode_id: str, values: dict) -> dict:
                    if episode_id != "episode-1":
                        raise AssertionError(episode_id)
                    self.episode["attributes"].update(values)
                    self.episode["attributes"]["formatted_description"] = description
                    return self.episode

            plan = {
                "description_actions": [
                    {
                        "episode_id": "episode-1",
                        "episode_status": "published",
                        "video_id": "AAAAAAAAAAA",
                        "description_path": relative_source,
                        "description_sha256": sha256_text(description),
                        "remote_description_sha256": sha256_text("Old show notes"),
                    }
                ]
            }

            with self.assertRaisesRegex(
                PlanPreconditionError,
                "left dynamic Show Notes tags unexpanded",
            ):
                apply_descriptions(FakeDescriptionClient(), plan, mock.Mock())

    def test_draft_cleanup_keeps_newest_duplicate_and_quarantines_unique(
        self,
    ) -> None:
        sync_plan = {
            "kind": "kedaibiao_podcast_sync_plan",
            "show_id": "show-one",
            "plan_hash": "source-hash",
            "blocked": [
                {
                    "scope": "publish",
                    "video_id": "duplicate01",
                    "reasons": [
                        "multiple_remote_drafts",
                        "youtube_candidate_not_verified",
                    ],
                },
                {
                    "scope": "publish",
                    "video_id": "unverified1",
                    "reasons": ["youtube_candidate_not_verified"],
                },
            ],
        }

        def episode(
            episode_id: str,
            video_id: str,
            created_at: str,
        ) -> dict:
            return {
                "id": episode_id,
                "attributes": {
                    "status": "draft",
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": video_id,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "media_url": f"https://example.com/{episode_id}.mp3",
                },
            }

        plan = build_cleanup_plan(
            sync_plan,
            [
                episode("10", "duplicate01", "2026-01-01T00:00:00Z"),
                episode("11", "duplicate01", "2026-02-01T00:00:00Z"),
                episode("12", "unverified1", "2026-03-01T00:00:00Z"),
            ],
        )
        self.assertEqual(
            plan["duplicate_groups"][0]["canonical_draft"]["episode_id"],
            "11",
        )
        self.assertEqual(
            [item["episode_id"] for item in plan["deletion_actions"]],
            ["10"],
        )
        self.assertEqual(
            plan["quarantined_unverified"][0]["recommendation"],
            "keep_unique_draft_quarantined",
        )


class TransistorClientTests(unittest.TestCase):
    def test_rate_limit_honors_retry_after_and_paginates(self) -> None:
        session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "1"}),
            FakeResponse(
                200,
                {
                    "data": [{"id": "one", "attributes": {}}],
                    "meta": {"totalPages": 2},
                },
            ),
            FakeResponse(
                200,
                {
                    "data": [{"id": "two", "attributes": {}}],
                    "meta": {"totalPages": 2},
                },
            ),
        ])
        sleep = mock.Mock()
        clock = iter([0.0, 0.0, 20.0, 20.0, 40.0, 40.0])
        client = TransistorClient(
            "test-key",
            session=session,
            sleep=sleep,
            monotonic=lambda: next(clock),
            min_interval=0,
        )
        episodes = client.list_episodes("show")
        self.assertEqual([episode["id"] for episode in episodes], ["one", "two"])
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(any(call.args[0] >= 10 for call in sleep.call_args_list))


class YtDlpUpdateTests(unittest.TestCase):
    @mock.patch.object(update_yt_dlp, "emit")
    @mock.patch.object(update_yt_dlp, "probe_version", side_effect=["2026.07.04", "2026.07.04"])
    @mock.patch.object(
        update_yt_dlp.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=["pip"],
            returncode=1,
            stdout="",
            stderr="network unavailable",
        ),
    )
    def test_update_failure_uses_still_runnable_current_version(
        self,
        _run: mock.Mock,
        _probe: mock.Mock,
        emit: mock.Mock,
    ) -> None:
        self.assertEqual(update_yt_dlp.main(), 0)
        emit.assert_called_once_with(
            "update_failed_using_current",
            version_before="2026.07.04",
            version_after="2026.07.04",
            reason="network unavailable",
        )


if __name__ == "__main__":
    unittest.main()
