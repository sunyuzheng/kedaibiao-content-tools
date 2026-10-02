"""Series-title integration using in-memory catalog and remote fixtures only."""

from __future__ import annotations

import copy
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from tools.check import build_podcast_sync_plan as planner
from tools.check import check_upload_quality as quality
from tools.podcast.core import sha256_text
from tools.upload import apply_podcast_sync_plan as executor
from tools.upload import reorder_episodes_by_date as reorder


A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"


def catalog_fixture() -> dict:
    return {
        A: {"series": "dialogue", "series_number": 1, "global_number": 1,
            "classification_basis": "Reviewed conversation"},
        B: {"series": "solo", "series_number": 1, "global_number": 2,
            "classification_basis": "Reviewed solo recording"},
        C: {"series": "dialogue", "series_number": 2,
            "classification_basis": "Reviewed new conversation"},
    }


def episode(video_id: str, number: int, title: str, *, status: str = "published") -> dict:
    return {
        "id": f"episode-{video_id}",
        "attributes": {
            "status": status, "number": number, "title": title,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "description": "Approved description", "image_url": "https://example.test/cover.jpg",
            "media_url": "https://example.test/audio.mp3", "published_at": "2026-09-18T00:00:00Z",
        },
    }


def local_payload(video_id: str, title: str = "New conversation") -> dict:
    return {
        "video_id": video_id, "base_title": title,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "image_url": "https://example.test/cover.jpg",
        "audio_path": "unused-audio.mp3", "audio_sha256": "unused",
        "published_at": "2026-09-18T00:00:00Z",
        "description_source": "youtube_snapshot", "description_path": None,
        "description_text": "Approved description",
        "description_sha256": sha256_text("Approved description"),
        "transcript_path": None,
    }


class PlannerSeriesTests(unittest.TestCase):
    def make_plan(self, catalog: dict, *, new_published: bool = False) -> dict:
        episodes = [episode(A, 1, "对话 001｜已编辑的对话"), episode(B, 2, "立正说 001｜独白")]
        if new_published:
            episodes.append(episode(C, 3, "对话 002｜New conversation"))
        records = [
            {"video_id": A, "upload_date": "20260916", "title": "Old local wording"},
            {"video_id": B, "upload_date": "20260917", "title": "Old local solo"},
            {"video_id": C, "upload_date": "20260918", "title": "New conversation",
             "action_needed": "none" if new_published else "publish_to_transistor"},
        ]
        by_video = {item["attributes"]["video_url"][-11:]: [item] for item in episodes}
        client = Mock()
        client.episodes_by_video_id.return_value = (episodes, by_video)
        youtube = {A: {"playlist_index": 3}, B: {"playlist_index": 2}, C: {"playlist_index": 1}}
        with ExitStack() as stack:
            mocks = {
                "load_env": None,
                "require_transistor_config": ("fake-test-key", "show"),
                "TransistorClient": client,
                "load_catalog": catalog,
                "current_youtube_state": (youtube, {"fresh": True}),
                "candidate_verification_state": ({C: {"status": "public"}}, {"fresh": True}),
                "local_records": records,
                "add_remote_and_actions": records,
            }
            for name, value in mocks.items():
                stack.enter_context(patch.object(planner, name, return_value=value))
            def frozen_local_payload(record, _, **kwargs):
                self.assertEqual(kwargs["include_promotion"], record["video_id"] not in by_video)
                return local_payload(record["video_id"], record["title"]), []

            stack.enter_context(patch.object(
                planner, "local_payload",
                side_effect=frozen_local_payload,
            ))
            return planner.build_plan(72)

    def test_new_series_number_is_independent_of_global_number(self) -> None:
        plan = self.make_plan(catalog_fixture())
        rows = {row["video_id"]: row for row in plan["projected_feed"]}
        self.assertEqual(rows[A]["target_title"], "对话 001｜已编辑的对话")
        self.assertEqual(rows[B]["target_title"], "立正说 001｜独白")
        self.assertEqual(rows[C]["target_title"], "对话 002｜New conversation")
        self.assertEqual(rows[C]["target_number"], 3)
        self.assertEqual([row["video_id"] for row in plan["projected_reorder_actions"]], [C])
        self.assertEqual(plan["publish_blocked_reasons"], [])

    def test_migrated_feed_has_no_historical_reorder(self) -> None:
        plan = self.make_plan(catalog_fixture(), new_published=True)
        self.assertEqual(plan["publish_actions"], [])
        self.assertEqual(plan["projected_reorder_actions"], [])
        self.assertEqual(plan["publish_blocked_reasons"], [])

    def test_unclassified_new_video_is_blocked_instead_of_defaulting_to_solo(self) -> None:
        catalog = catalog_fixture()
        del catalog[C]
        plan = self.make_plan(catalog)
        self.assertEqual(plan["publish_actions"], [])
        blocker = next(row for row in plan["blocked"] if row.get("video_id") == C)
        self.assertIn("series_assignment_missing_or_invalid", blocker["reasons"])

    def test_unclassified_history_disables_the_entire_projected_reorder(self) -> None:
        catalog = catalog_fixture()
        del catalog[B]
        plan = self.make_plan(catalog)
        self.assertIn("series_assignment_missing_or_invalid", plan["publish_blocked_reasons"])
        self.assertEqual(plan["projected_feed"], [])
        self.assertEqual(plan["projected_reorder_actions"], [])


class ReorderSeriesTests(unittest.TestCase):
    def test_series_titles_preserve_global_numbers_and_current_body(self) -> None:
        client = Mock()
        client.list_episodes.return_value = [episode(A, 1, "E1. Edited dialogue"), episode(B, 2, "E2. Solo")]
        with patch.object(reorder, "load_catalog", return_value=catalog_fixture()), patch.object(
            reorder, "build_local_date_map", return_value={A: "20260916", B: "20260917"}
        ):
            plan = reorder.build_reorder_plan(client, "show")
        self.assertEqual([a["target_title"] for a in plan["actions"]], ["对话 001｜Edited dialogue", "立正说 001｜Solo"])
        self.assertTrue(all(a["current_number"] == a["target_number"] for a in plan["actions"]))
        client.update_episode.assert_not_called()

    def test_missing_assignment_blocks_all_actions(self) -> None:
        client = Mock()
        client.list_episodes.return_value = [episode(A, 1, "E1. Dialogue"), episode(B, 2, "E2. Solo")]
        with patch.object(reorder, "load_catalog", return_value={A: catalog_fixture()[A]}), patch.object(
            reorder, "build_local_date_map", return_value={A: "20260916", B: "20260917"}
        ):
            plan = reorder.build_reorder_plan(client, "show")
        self.assertEqual(plan["actions"], [])
        self.assertIn("series_assignment_missing_or_invalid", plan["blocked_reasons"])
        self.assertEqual(plan["series_errors"][0]["video_id"], B)

    def test_migrated_titles_do_not_receive_another_prefix(self) -> None:
        client = Mock()
        client.list_episodes.return_value = [episode(A, 1, "对话 001｜Edited dialogue"), episode(B, 2, "立正说 001｜Solo")]
        with patch.object(reorder, "load_catalog", return_value=catalog_fixture()), patch.object(
            reorder, "build_local_date_map", return_value={A: "20260916", B: "20260917"}
        ):
            plan = reorder.build_reorder_plan(client, "show")
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["blocked_reasons"], [])


class MemoryClient:
    def __init__(self, *, draft: bool) -> None:
        self.episode = episode(C, 532, "Old draft", status="draft") if draft else None
        self.creates: list[dict] = []
        self.updates: list[dict] = []

    def episodes_by_video_id(self, _show: str) -> tuple[list, dict]:
        return ([self.episode], {C: [self.episode]}) if self.episode else ([], {})

    def authorize_upload(self, _name: str) -> dict:
        return {"upload_url": "unused", "content_type": "audio/mpeg", "audio_url": "unused"}

    def upload_audio(self, *_args) -> None:
        pass

    def create_episode(self, payload: dict) -> dict:
        self.creates.append(copy.deepcopy(payload))
        self.episode = episode(C, 532, payload["title"], status="draft")
        self.episode["attributes"].update(payload)
        return self.episode

    def update_episode(self, _episode_id: str, payload: dict) -> dict:
        self.updates.append(copy.deepcopy(payload))
        self.episode["attributes"].update(payload)
        return self.episode

    def get_episode(self, _episode_id: str) -> dict:
        return self.episode

    def publish_episode(self, _episode_id: str, date: str) -> None:
        self.episode["attributes"].update(status="published", published_at=date)

    def list_episodes(self, _show: str) -> list:
        return [self.episode] if self.episode else []


class FrozenPublishTitleTests(unittest.TestCase):
    @staticmethod
    def plan(*, draft: bool = False) -> dict:
        return {
            "youtube_snapshot": {"fresh": True}, "publish_blocked_reasons": [],
            "publish_actions": [{
                "action": "update_draft_then_publish" if draft else "create_draft_then_publish",
                "local": local_payload(C, "Different local body"),
                "remote_precondition": {"episode_id": f"episode-{C}"} if draft else None,
            }],
            "projected_feed": [{
                "video_id": C, "planned_publish": True,
                "target_title": "对话 037｜Exact approved wording", "target_number": 532,
            }],
            "projected_reorder_actions": [],
        }

    def test_create_and_repair_publish_frozen_title_without_catalog_recomputation(self) -> None:
        for draft in (False, True):
            with self.subTest(draft=draft):
                client = MemoryClient(draft=draft)
                plan = self.plan(draft=draft)
                with patch.object(executor, "verify_audio", return_value=Path("unused.mp3")), patch(
                    "tools.podcast.series.load_catalog", side_effect=AssertionError("Must use approved title")
                ):
                    published, repaired = executor.apply_publish(client, "show", plan, Mock())
                self.assertEqual((published, repaired), (1, int(draft)))
                self.assertEqual(client.episode["attributes"]["title"], "对话 037｜Exact approved wording")
                self.assertEqual(client.episode["attributes"]["number"], 532)
                self.assertTrue(all(p["title"] == "对话 037｜Exact approved wording" for p in client.updates))
                self.assertTrue(all(p["number"] == 532 for p in client.updates))
                if not draft:
                    self.assertEqual(client.creates[0]["title"], "对话 037｜Exact approved wording")
                    self.assertEqual(client.creates[0]["number"], 532)
                    self.assertNotIn("increment_number", client.creates[0])

    def test_missing_or_ambiguous_frozen_titles_fail_before_remote_effects(self) -> None:
        for mode in ("missing", "blank", "duplicate", "historical_row", "second_target_missing"):
            with self.subTest(mode=mode):
                plan = self.plan()
                if mode == "missing":
                    plan["projected_feed"] = []
                elif mode == "blank":
                    plan["projected_feed"][0]["target_title"] = "  "
                elif mode == "duplicate":
                    plan["projected_feed"].append(copy.deepcopy(plan["projected_feed"][0]))
                elif mode == "historical_row":
                    plan["projected_feed"][0]["planned_publish"] = False
                else:
                    second = copy.deepcopy(plan["publish_actions"][0])
                    second["local"]["video_id"] = B
                    plan["publish_actions"].append(second)
                client = Mock()
                with self.assertRaises(executor.PlanPreconditionError):
                    executor.apply_publish(client, "show", plan, Mock())
                self.assertEqual(client.mock_calls, [])


class SeriesQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = {C: {"series": "dialogue", "series_number": 37, "global_number": 532,
                            "classification_basis": "Reviewed conversation"}}

    def check(self, item: dict, *, catalog: dict | None = None) -> list[str]:
        return quality.check_episode(item, None, require_published=True,
                                     catalog=self.catalog if catalog is None else catalog)

    def test_series_prefix_is_independent_of_global_number_and_edited_body(self) -> None:
        self.assertEqual(self.check(episode(C, 532, "对话 037｜New editorial title")), [])

    def test_wrong_series_prefix_or_sequence_fails(self) -> None:
        for title in ("E532. Body", "立正说 037｜Body", "对话 038｜Body", "对话 037｜"):
            with self.subTest(title=title):
                self.assertTrue(self.check(episode(C, 532, title)))

    def test_known_global_number_drift_and_invalid_numbers_fail(self) -> None:
        for number in (531, 0, -1, None, True, 532.5, "532"):
            with self.subTest(number=number):
                self.assertTrue(self.check(episode(C, number, "对话 037｜Body")))

    def test_missing_classification_fails(self) -> None:
        issues = self.check(episode(C, 532, "对话 037｜Body"), catalog={})
        self.assertTrue(any("系列分类缺失或无效" in issue for issue in issues))

    def test_future_assignment_without_locked_global_number_accepts_positive_number(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        del catalog[C]["global_number"]
        self.assertEqual(self.check(episode(C, 533, "对话 037｜Body"), catalog=catalog), [])


if __name__ == "__main__":
    unittest.main()
