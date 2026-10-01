from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import tools.automation.sync_podcast as sync_podcast
from tools.automation.email_notification import build_message
from tools.automation.podcast_auto_publish import evaluate_auto_publish
from tools.automation.podcast_sync_report import (
    build_completion_report,
    build_execution_failure_report,
    build_reconciliation_report,
)
from tools.automation.sync_podcast import (
    AutoPublishExecutionError,
    check_public_feed,
    execute_auto_publish,
    execution_mode,
    notification_delivery_enabled,
)
from tools.podcast.core import (
    PROJECT_ROOT,
    canonical_json,
    plan_hash,
    sha256_file,
    sha256_text,
)
from tools.podcast.show_notes import (
    PORTABLE_HTML_FORMAT,
    render_portable_show_notes_html,
)


NOW = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)


def finalize_plan(plan: dict) -> dict:
    scope = {
        "kind": "podcast_publish",
        "show_id": plan.get("show_id"),
        "youtube_snapshot": plan.get("youtube_snapshot"),
        "items": plan.get("publish_actions", []),
        "projected_feed": plan.get("projected_feed", []),
        "projected_reorder_actions": plan.get("projected_reorder_actions", []),
        "publish_blocked_reasons": plan.get("publish_blocked_reasons", []),
    }
    plan["publish_approval_hash"] = sha256_text(canonical_json(scope))
    plan["plan_hash"] = plan_hash(plan)
    return plan


def eligible_plan(root: Path, video_ids: list[str] | None = None) -> dict:
    video_ids = video_ids or ["AAAAAAAAAAA"]
    audio = root / "episode.m4a"
    audio.write_bytes(b"safe-audio")
    relative_audio = str(audio.relative_to(PROJECT_ROOT))
    source = (
        "这是一段足够完整的节目简介，说明本期内容与听众的关系。" * 12
        + "\n\nhttps://www.superlinear.academy/"
    )
    rendered = render_portable_show_notes_html(source)
    actions = []
    projected = []
    for index, video_id in enumerate(video_ids, 1):
        actions.append(
            {
                "action": "create_draft_then_publish",
                "local": {
                    "video_id": video_id,
                    "youtube_privacy": "public",
                    "content_class": "normal_video",
                    "podcast_policy": "ready_public_normal",
                    "playlist_index": index,
                    "folder": str(root.relative_to(PROJECT_ROOT)),
                    "audio_path": relative_audio,
                    "audio_sha256": sha256_file(audio),
                    "audio_bytes": audio.stat().st_size,
                    "description_path": None,
                    "description_source": "youtube_snapshot",
                    "description_format": PORTABLE_HTML_FORMAT,
                    "description_source_sha256": sha256_text(source),
                    "description_source_chars": len(source),
                    "description_text": source,
                    "description_sha256": sha256_text(rendered),
                    "description_chars": len(rendered),
                    "transcript_path": None,
                    "transcript_source_status": "missing",
                    "transcript_sha256": None,
                    "transcript_chars": 0,
                    "base_title": f"Episode {index}",
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "image_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    "published_at": "2026-08-23T12:00:00Z",
                },
                "remote_precondition": None,
                "warnings": ["missing_transcript"],
                "youtube_verification": {
                    "status": "public",
                    "verified_at": "2026-08-23T16:45:00+00:00",
                },
            }
        )
        projected.append(
            {
                "video_id": video_id,
                "date": "20260823",
                "base_title": f"Episode {index}",
                "episode_id": None,
                "planned_publish": True,
                "current_number": None,
                "current_title": None,
                "target_number": 522 + index,
                "target_title": f"E{522 + index} Episode {index}",
            }
        )
    plan = {
        "schema_version": 2,
        "kind": "kedaibiao_podcast_sync_plan",
        "generated_at": "2026-08-23T16:50:00+00:00",
        "show_id": "show-one",
        "youtube_snapshot": {
            "fresh": True,
            "max_age_hours": 72,
            "candidate_verification": {"fresh": True, "max_age_hours": 72},
        },
        "incremental_publish_baseline": {
            "available": True,
            "video_id": "ZZZZZZZZZZZ",
            "playlist_index": 10,
        },
        "publish_actions": actions,
        "candidate_publish_actions": actions,
        "projected_feed": projected,
        "projected_reorder_actions": projected,
        "publish_blocked_reasons": [],
        "description_actions": [],
        "transcript_actions": [],
        "blocked": [],
    }
    return finalize_plan(plan)


class AutoPublishPolicyTests(unittest.TestCase):
    def test_public_normal_incremental_fallback_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            plan = eligible_plan(Path(directory))
            decision = evaluate_auto_publish(plan, PROJECT_ROOT, now=NOW)

        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["blockers"], [])
        warning_codes = {item["warning"] for item in decision["receipt_warnings"]}
        self.assertIn("show_notes_fallback_source", warning_codes)
        self.assertIn("missing_transcript", warning_codes)

    def test_dry_run_never_selects_executor(self) -> None:
        self.assertEqual(
            execution_mode({"eligible": True}, dry_run=True),
            "dry_run",
        )
        self.assertEqual(
            execution_mode({"eligible": True}, dry_run=False),
            "execute",
        )

    def test_batch_limit_and_unknown_warning_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            ids = [f"AAAAAAA{i:04d}" for i in range(4)]
            plan = eligible_plan(root, ids)
            decision = evaluate_auto_publish(plan, PROJECT_ROOT, max_items=3, now=NOW)
            self.assertFalse(decision["eligible"])
            self.assertIn(
                "auto_publish_batch_limit_exceeded",
                {item["code"] for item in decision["blockers"]},
            )

            plan = eligible_plan(root)
            plan["publish_actions"][0]["warnings"].append("future_unknown_warning")
            finalize_plan(plan)
            decision = evaluate_auto_publish(plan, PROJECT_ROOT, now=NOW)
            self.assertIn(
                "candidate_has_hard_quality_warning",
                {item["code"] for item in decision["blockers"]},
            )

    def test_hard_batch_limit_cannot_be_raised(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            plan = eligible_plan(
                Path(directory),
                [f"AAAAAAA{i:04d}" for i in range(4)],
            )
            with self.assertRaisesRegex(ValueError, "hard auto-publish safety limit"):
                evaluate_auto_publish(plan, PROJECT_ROOT, max_items=4, now=NOW)

    def test_dry_run_disables_all_notification_delivery(self) -> None:
        self.assertFalse(
            notification_delivery_enabled(no_notification=False, dry_run=True)
        )
        self.assertFalse(
            notification_delivery_enabled(no_notification=True, dry_run=False)
        )
        self.assertTrue(
            notification_delivery_enabled(no_notification=False, dry_run=False)
        )

    def test_artifact_drift_and_historical_reorder_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            plan = eligible_plan(root)
            (root / "episode.m4a").write_bytes(b"changed")
            decision = evaluate_auto_publish(plan, PROJECT_ROOT, now=NOW)
            self.assertIn(
                "audio_artifact_changed",
                {item["code"] for item in decision["blockers"]},
            )

            plan = eligible_plan(root)
            plan["projected_reorder_actions"].append(
                {
                    "video_id": "OLDOLDOLD12",
                    "planned_publish": False,
                    "target_number": 1,
                    "target_title": "E1 old",
                }
            )
            finalize_plan(plan)
            decision = evaluate_auto_publish(plan, PROJECT_ROOT, now=NOW)
            self.assertIn(
                "historical_episode_reorder_not_automatic",
                {item["code"] for item in decision["blockers"]},
            )

    def test_public_and_normal_are_verified_from_immutable_payload(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            plan = eligible_plan(Path(directory))
            plan["publish_actions"][0]["local"]["content_class"] = "live_replay"
            finalize_plan(plan)
            decision = evaluate_auto_publish(plan, PROJECT_ROOT, now=NOW)
        self.assertIn(
            "candidate_policy_not_public_normal",
            {item["code"] for item in decision["blockers"]},
        )


class NotificationStateTests(unittest.TestCase):
    def test_planner_and_auto_gate_same_problem_render_once(self) -> None:
        plan = finalize_plan(
            {
                "kind": "kedaibiao_podcast_sync_plan",
                "show_id": "show-one",
                "youtube_snapshot": {"fresh": False},
                "publish_actions": [],
                "projected_feed": [],
                "projected_reorder_actions": [],
                "publish_blocked_reasons": [],
                "description_actions": [],
                "transcript_actions": [],
                "blocked": [
                    {
                        "scope": "publish",
                        "video_id": None,
                        "title": "公开 feed",
                        "reasons": ["stale_youtube_snapshot"],
                    }
                ],
            }
        )
        summary = {"plan_hash": plan["plan_hash"], "plan_path": "plan.json"}
        report, _ = build_reconciliation_report(
            summary=summary,
            plan=plan,
            project_root=PROJECT_ROOT,
            started_at="start",
            finished_at="finish",
            auto_decision={
                "has_candidates": False,
                "eligible": False,
                "blockers": [
                    {
                        "code": "youtube_snapshot_stale",
                        "video_id": None,
                        "context": "YouTube 当前公共清单证据已过期。",
                        "impact": "不能安全发布。",
                        "action": "刷新清单。",
                        "location": "public_videos_snapshot.json",
                    }
                ],
            },
        )
        self.assertEqual(len(report["action_items"]), 1)

    def test_unchanged_historical_gaps_are_silent(self) -> None:
        plan = finalize_plan(
            {
                "kind": "kedaibiao_podcast_sync_plan",
                "show_id": "show-one",
                "youtube_snapshot": {"fresh": True},
                "publish_actions": [],
                "projected_feed": [],
                "projected_reorder_actions": [],
                "publish_blocked_reasons": [],
                "description_actions": [],
                "transcript_actions": [],
                "blocked": [
                    {
                        "scope": "publish",
                        "video_id": f"historic-{index}",
                        "reasons": ["historical_gap_requires_backfill_mode"],
                    }
                    for index in range(21)
                ],
            }
        )
        summary = {"plan_hash": plan["plan_hash"], "plan_path": "plan.json"}
        report, state = build_reconciliation_report(
            summary=summary,
            plan=plan,
            project_root=PROJECT_ROOT,
            started_at="start",
            finished_at="finish",
            auto_decision={"has_candidates": False, "eligible": False, "blockers": []},
        )
        self.assertEqual(report["status"], "healthy")
        self.assertFalse(report["should_notify"])
        self.assertEqual(report["background_blocked_count"], 21)
        self.assertNotIn("histor", build_message(report)["text"].lower())
        self.assertEqual(state["actionable_problem_keys"], [])

    def test_maintenance_is_deduplicated_separately(self) -> None:
        plan = finalize_plan(
            {
                "kind": "kedaibiao_podcast_sync_plan",
                "show_id": "show-one",
                "youtube_snapshot": {
                    "fresh": True,
                    "oauth_snapshot": {
                        "exists": True,
                        "fresh": False,
                        "age_hours": 300,
                    },
                },
                "publish_actions": [],
                "projected_feed": [],
                "projected_reorder_actions": [],
                "publish_blocked_reasons": [],
                "description_actions": [],
                "transcript_actions": [],
                "blocked": [],
            }
        )
        summary = {"plan_hash": plan["plan_hash"], "plan_path": "plan.json"}
        first, state = build_reconciliation_report(
            summary=summary,
            plan=plan,
            project_root=PROJECT_ROOT,
            started_at="start",
            finished_at="finish",
            auto_decision={"has_candidates": False, "eligible": False, "blockers": []},
        )
        second, _ = build_reconciliation_report(
            summary=summary,
            plan=plan,
            project_root=PROJECT_ROOT,
            started_at="start2",
            finished_at="finish2",
            previous_state=state,
            auto_decision={"has_candidates": False, "eligible": False, "blockers": []},
        )
        self.assertEqual(first["status"], "maintenance")
        self.assertTrue(first["should_notify"])
        self.assertEqual(second["status"], "healthy")
        self.assertFalse(second["should_notify"])


class AutoPublishExecutionTests(unittest.TestCase):
    def test_success_requires_quality_reorder_and_rebuild_before_completion(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            plan = eligible_plan(root)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            rebuilt_path = root / "rebuilt.json"
            rebuilt_path.write_text(
                json.dumps({"publish_actions": [], "candidate_publish_actions": []}),
                encoding="utf-8",
            )
            calls: list[str] = []

            def stream_runner(command, _log):
                calls.append(Path(command[1]).name)
                if any(str(part).endswith("apply_podcast_sync_plan.py") for part in command):
                    return [
                        json.dumps(
                            {
                                "event": "episode_published",
                                "video_id": "AAAAAAAAAAA",
                                "episode_id": "episode-523",
                                "published_at": "2026-08-23T12:00:00Z",
                                "transcript_verification": "not_requested",
                            }
                        ),
                        f"Ledger: {root / 'ledger.jsonl'}",
                    ]
                return []

            json_calls = 0

            def json_runner(command, _log):
                nonlocal json_calls
                json_calls += 1
                if any(str(part).endswith("reorder_episodes_by_date.py") for part in command):
                    return {"blocked_reasons": [], "action_count": 0}
                return {"plan_path": str(rebuilt_path)}

            execution = execute_auto_publish(
                plan_path=plan_path,
                plan=plan,
                log=object(),
                max_snapshot_age_hours=72,
                stream_runner=stream_runner,
                json_runner=json_runner,
                feed_checker=lambda _ids: {
                    "status": "observed",
                    "detail": "RSS observed",
                },
            )

        self.assertEqual(calls, ["apply_podcast_sync_plan.py", "check_upload_quality.py"])
        self.assertEqual(json_calls, 2)
        base = {
            "status": "ready_for_auto_publish",
            "publish_items": [
                {"video_id": "AAAAAAAAAAA", "title": "E523 Episode 1"}
            ],
            "action_items": [],
        }
        completed = build_completion_report(base, execution, finished_at="done")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["publish_items"][0]["episode_id"], "episode-523")
        self.assertIn("已自动发布", build_message(completed)["subject"])

    def test_partial_failure_never_reports_completion(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            plan = eligible_plan(root)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            def failing_runner(_command, _log):
                line = json.dumps(
                    {
                        "event": "episode_published",
                        "video_id": "AAAAAAAAAAA",
                        "episode_id": "episode-523",
                        "published_at": "2026-08-23T12:00:00Z",
                    }
                )
                raise subprocess.CalledProcessError(1, ["executor"], output=line)

            with self.assertRaises(AutoPublishExecutionError) as caught:
                execute_auto_publish(
                    plan_path=plan_path,
                    plan=plan,
                    log=object(),
                    max_snapshot_age_hours=72,
                    stream_runner=failing_runner,
                )

        self.assertEqual(len(caught.exception.partial_publications), 1)
        report = build_execution_failure_report(
            {"status": "ready_for_auto_publish", "action_items": []},
            stage=caught.exception.stage,
            error=str(caught.exception),
            partial_publications=caught.exception.partial_publications,
            log_path="ledger.jsonl",
            finished_at="done",
        )
        message = build_message(report)
        self.assertEqual(report["status"], "action_required")
        self.assertNotIn("已自动发布", message["subject"])
        self.assertIn("不要重复创建", message["text"])

    def test_default_feed_checker_is_resolved_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            plan = eligible_plan(root)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            rebuilt_path = root / "rebuilt.json"
            rebuilt_path.write_text(
                json.dumps({"publish_actions": [], "candidate_publish_actions": []}),
                encoding="utf-8",
            )

            def stream_runner(command, _log):
                if any(str(part).endswith("apply_podcast_sync_plan.py") for part in command):
                    return [
                        json.dumps(
                            {
                                "event": "episode_published",
                                "video_id": "AAAAAAAAAAA",
                                "episode_id": "episode-523",
                                "published_at": "2026-08-23T12:00:00Z",
                            }
                        )
                    ]
                return []

            def json_runner(command, _log):
                if any(str(part).endswith("reorder_episodes_by_date.py") for part in command):
                    return {"blocked_reasons": [], "action_count": 0}
                return {"plan_path": str(rebuilt_path)}

            with mock.patch.object(
                sync_podcast,
                "check_public_feed",
                return_value={"status": "observed", "detail": "RSS observed"},
            ) as checker:
                execute_auto_publish(
                    plan_path=plan_path,
                    plan=plan,
                    log=object(),
                    max_snapshot_age_hours=72,
                    stream_runner=stream_runner,
                    json_runner=json_runner,
                )

        checker.assert_called_once_with(
            ["AAAAAAAAAAA"],
            expected_titles={"AAAAAAAAAAA": "E523 Episode 1"},
        )


class PublicFeedReceiptTests(unittest.TestCase):
    def test_legacy_id_only_mode_still_observes_video_id(self) -> None:
        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
                <rss><channel><item>
                <title>E524. A valid episode</title>
                <description>Watch AAAAAAAAAAA</description>
                </item></channel></rss>"""

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        receipt = check_public_feed(["AAAAAAAAAAA"], session=Session())

        self.assertEqual(receipt["status"], "observed")

    def test_exact_rss_title_proves_propagation_without_youtube_id(self) -> None:
        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
                <rss><channel><item>
                <title>E524. A valid episode</title>
                <guid>episode-guid</guid>
                </item></channel></rss>"""

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        receipt = check_public_feed(
            ["AAAAAAAAAAA"],
            expected_titles={"AAAAAAAAAAA": "E524. A valid episode"},
            session=Session(),
        )

        self.assertEqual(receipt["status"], "observed")
        self.assertEqual(receipt["observed_video_ids"], ["AAAAAAAAAAA"])

    def test_title_substring_does_not_count_as_exact_item_title(self) -> None:
        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
                <rss><channel><item>
                <title>E524. A valid episode extended</title>
                </item></channel></rss>"""

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        receipt = check_public_feed(
            ["AAAAAAAAAAA"],
            expected_titles={"AAAAAAAAAAA": "E524. A valid episode"},
            session=Session(),
        )

        self.assertEqual(receipt["status"], "propagation_pending")

    def test_old_episode_reference_does_not_prove_new_episode_exists(self) -> None:
        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
                <rss><channel><item>
                <title>E100. An old episode</title>
                <description>Related video: AAAAAAAAAAA</description>
                </item></channel></rss>"""

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        receipt = check_public_feed(
            ["AAAAAAAAAAA"],
            expected_titles={"AAAAAAAAAAA": "E524. A new episode"},
            session=Session(),
        )

        self.assertEqual(receipt["status"], "propagation_pending")

    def test_missing_expected_title_fails_closed(self) -> None:
        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
                <rss><channel><item>
                <title>E100. An old episode</title>
                <description>Related video: AAAAAAAAAAA</description>
                </item></channel></rss>"""

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        receipt = check_public_feed(
            ["AAAAAAAAAAA"],
            expected_titles={},
            session=Session(),
        )

        self.assertEqual(receipt["status"], "propagation_pending")


if __name__ == "__main__":
    unittest.main()
