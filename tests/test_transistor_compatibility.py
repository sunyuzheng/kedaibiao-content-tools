"""Current/legacy YouTube identity and uncertain-write recovery, without network."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from tools.check import build_podcast_sync_plan as planner
from tools.check import check_upload_quality as quality
from tools.podcast.core import (
    episode_video_id,
    episode_youtube_url,
    extract_video_id,
    sha256_text,
)
from tools.podcast.transistor_client import AmbiguousMutationError, TransistorClient
from tools.upload import apply_podcast_sync_plan as executor
from tools.upload import reorder_episodes_by_date as reorder


VIDEO_ID = "AAAAAAAAAAA"
YOUTUBE_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


class Response:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self.payload = payload or {}
        self.headers: dict[str, str] = {}
        self.text = ""

    def json(self) -> dict:
        return copy.deepcopy(self.payload)


class Session:
    def __init__(self, outcomes: list) -> None:
        self.headers: dict[str, str] = {}
        self.outcomes = iter(outcomes)
        self.calls: list = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_for(session) -> TransistorClient:
    return TransistorClient("test-only-key", session=session, min_interval=0, sleep=Mock())


class IdentityTests(unittest.TestCase):
    def test_only_recognized_youtube_urls_or_bare_ids_parse(self) -> None:
        for value in (
            VIDEO_ID, YOUTUBE_URL, f"https://youtu.be/{VIDEO_ID}?t=30",
            f"https://m.youtube.com/watch?list=123&v={VIDEO_ID}",
            f"https://www.youtube.com/shorts/{VIDEO_ID}",
        ):
            with self.subTest(value=value):
                self.assertEqual(extract_video_id(value), VIDEO_ID)
        for value in (
            f"https://media.example/{VIDEO_ID}",
            f"https://media.example/watch?v={VIDEO_ID}",
            f"https://youtube.com.evil.example/watch?v={VIDEO_ID}",
            f"https://youtube.com@evil.example/watch?v={VIDEO_ID}",
            f"https://youtube.com/watch?v={VIDEO_ID}&v=BBBBBBBBBBB",
            f"https://evil.example/youtu.be/{VIDEO_ID}",
        ):
            with self.subTest(value=value):
                self.assertIsNone(extract_video_id(value))

    def test_modern_preferred_and_only_valid_legacy_urls_are_fallbacks(self) -> None:
        attrs = {"youtube_url": YOUTUBE_URL, "video_url": f"https://youtu.be/{VIDEO_ID}"}
        self.assertEqual(episode_youtube_url(attrs), YOUTUBE_URL)
        self.assertEqual(episode_video_id({"video_url": YOUTUBE_URL}), VIDEO_ID)
        self.assertEqual(episode_video_id({"youtube_url": YOUTUBE_URL, "video_url": "https://media.example/a.mp4"}), VIDEO_ID)
        self.assertEqual(episode_video_id({"youtube_url": "invalid", "video_url": YOUTUBE_URL}), VIDEO_ID)
        self.assertIsNone(episode_video_id({"video_url": VIDEO_ID}))
        self.assertIsNone(episode_video_id({"video_url": f"https://media.example/{VIDEO_ID}"}))

    def test_conflicts_stop_mapping_planning_and_execution(self) -> None:
        episode = {"id": "one", "attributes": {
            "youtube_url": YOUTUBE_URL,
            "video_url": "https://youtu.be/BBBBBBBBBBB",
        }}
        with self.assertRaises(ValueError):
            planner.compact_episode(episode)
        with self.assertRaises(executor.PlanPreconditionError):
            executor.verify_episode_video(episode, VIDEO_ID)
        session = Session([Response(200, {"data": [episode]})])
        with self.assertRaises(ValueError):
            client_for(session).episodes_by_video_id("show")

    def test_collection_and_frozen_plan_accept_modern_or_legacy_fields(self) -> None:
        episodes = [
            {"id": "modern", "attributes": {"youtube_url": YOUTUBE_URL}},
            {"id": "legacy", "attributes": {"video_url": "https://youtu.be/BBBBBBBBBBB"}},
            {"id": "media", "attributes": {"video_url": "https://media.example/CCCCCCCCCCC"}},
        ]
        session = Session([Response(200, {"data": episodes})])
        _, by_video = client_for(session).episodes_by_video_id("show")
        self.assertEqual(set(by_video), {VIDEO_ID, "BBBBBBBBBBB"})
        self.assertEqual(planner.compact_episode(episodes[0])["video_url"], YOUTUBE_URL)
        executor.verify_episode_video(episodes[0], VIDEO_ID)

    def test_modern_identity_passes_quality_and_reorder(self) -> None:
        episode = {"id": "one", "attributes": {
            "youtube_url": YOUTUBE_URL, "video_url": "https://media.example/video.mp4",
            "number": 1, "status": "published", "published_at": "2026-09-01T00:00:00Z",
            "title": "立正说 001｜Title", "description": "Description", "image_url": "https://example.test/image.jpg",
        }}
        catalog = {VIDEO_ID: {"series": "solo", "series_number": 1, "global_number": 1}}
        self.assertEqual(quality.check_episode(episode, None, require_published=True, catalog=catalog), [])
        client = Mock()
        client.list_episodes.return_value = [episode]
        with patch.object(reorder, "build_local_date_map", return_value={VIDEO_ID: "20260901"}), patch.object(reorder, "load_catalog", return_value=catalog):
            plan = reorder.build_reorder_plan(client, "show")
        self.assertEqual(plan["blocked_reasons"], [])
        self.assertEqual(plan["actions"], [])


class RetryTests(unittest.TestCase):
    def test_uncertain_create_is_sent_exactly_once(self) -> None:
        for outcome in (requests.ReadTimeout("response lost"), Response(500), Response(503)):
            with self.subTest(outcome=type(outcome).__name__):
                session = Session([outcome, Response(201, {"data": {"id": "duplicate"}})])
                with self.assertRaises(AmbiguousMutationError):
                    client_for(session).create_episode({"youtube_url": YOUTUBE_URL})
                self.assertEqual(len(session.calls), 1)

    def test_uncertain_publish_is_not_replayed(self) -> None:
        session = Session([requests.ReadTimeout("response lost")])
        with self.assertRaises(AmbiguousMutationError):
            client_for(session).publish_episode("one", "2026-09-01T00:00:00Z")
        self.assertEqual(len(session.calls), 1)

    def test_read_retries_and_explicit_rate_limit_retries_remain(self) -> None:
        session = Session([requests.ReadTimeout(), Response(503), Response(200, {"data": {"id": "one"}})])
        self.assertEqual(client_for(session).get_episode("one")["id"], "one")
        self.assertEqual(len(session.calls), 3)
        session = Session([Response(429), Response(201, {"data": {"id": "one"}})])
        self.assertEqual(client_for(session).create_episode({"youtube_url": YOUTUBE_URL})["id"], "one")
        self.assertEqual(len(session.calls), 2)


class HistoricalFeedPreflightTests(unittest.TestCase):
    @staticmethod
    def fixture() -> tuple[list[dict], dict]:
        episodes = [{"id": f"episode-{video_id}", "attributes": {
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "number": number, "title": f"Historical {number}", "status": "published",
        }} for number, video_id in enumerate((VIDEO_ID, "CCCCCCCCCCC"), 1)]
        history = [{
            "video_id": episode_video_id(ep["attributes"]), "episode_id": ep["id"],
            "planned_publish": False, "current_number": ep["attributes"]["number"],
            "current_title": ep["attributes"]["title"],
            "target_number": ep["attributes"]["number"], "target_title": ep["attributes"]["title"],
        } for ep in episodes]
        plan = {
            "youtube_snapshot": {"fresh": True}, "publish_blocked_reasons": [],
            "publish_actions": [{"action": "create_draft_then_publish", "local": {"video_id": "BBBBBBBBBBB"}}],
            "projected_feed": [*history, {
                "video_id": "BBBBBBBBBBB", "planned_publish": True,
                "target_title": "New target", "target_number": 3,
            }],
        }
        return episodes, plan

    def test_historical_drift_blocks_before_upload_or_mutation(self) -> None:
        for drift in ("extra", "missing", "title", "number", "replacement", "duplicate", "unidentified", "conflict"):
            with self.subTest(drift=drift):
                episodes, plan = self.fixture()
                if drift == "extra":
                    episodes.append({"id": "extra", "attributes": {"status": "published", "youtube_url": "https://youtu.be/DDDDDDDDDDD"}})
                elif drift == "missing":
                    episodes.pop()
                elif drift in {"title", "number"}:
                    episodes[-1]["attributes"][drift] = "changed"
                elif drift == "replacement":
                    episodes[-1]["id"] = "replacement"
                elif drift == "duplicate":
                    episodes.append(copy.deepcopy(episodes[-1]))
                elif drift == "unidentified":
                    episodes[-1]["attributes"].pop("youtube_url")
                else:
                    episodes[-1]["attributes"]["video_url"] = YOUTUBE_URL
                client = Mock()
                client.episodes_by_video_id.return_value = (episodes, {})
                with self.assertRaises(executor.PlanPreconditionError):
                    executor.apply_publish(client, "show", plan, Mock())
                self.assertEqual([call[0] for call in client.method_calls], ["episodes_by_video_id"])

    def test_unchanged_history_accepts_legacy_response_and_new_draft(self) -> None:
        episodes, plan = self.fixture()
        episodes[0]["attributes"]["video_url"] = episodes[0]["attributes"].pop("youtube_url")
        episodes.append({"id": "draft", "attributes": {"status": "draft", "youtube_url": "https://youtu.be/BBBBBBBBBBB"}})
        executor.verify_published_feed_preconditions(episodes, plan["projected_feed"])

    def test_already_published_target_requires_fresh_plan_before_mutation(self) -> None:
        episodes, plan = self.fixture()
        episodes.append({"id": "newly-published", "attributes": {
            "status": "published", "youtube_url": "https://youtu.be/BBBBBBBBBBB",
            "number": 3, "title": "New target",
        }})
        client = Mock()
        client.episodes_by_video_id.return_value = (episodes, {})
        with self.assertRaises(executor.PlanPreconditionError):
            executor.apply_publish(client, "show", plan, Mock())
        self.assertEqual([call[0] for call in client.method_calls], ["episodes_by_video_id"])


class CreatedButResponseLostSession:
    """The first POST succeeds on the server, then loses its response."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.episode: dict | None = None
        self.create_count = 0
        self.writes: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/episodes"):
            return Response(200, {"data": [self.episode] if self.episode else []})
        if method == "POST":
            self.create_count += 1
            payload = kwargs["json"]["episode"]
            self.writes.append(copy.deepcopy(payload))
            self.episode = {"id": "one", "attributes": {
                **payload, "number": 1, "status": "draft", "media_url": "https://example.test/audio.mp3",
            }}
            raise requests.ReadTimeout("server created the draft, response lost")
        if method == "PATCH":
            if url.endswith("/publish"):
                self.episode["attributes"].update(status="published", published_at=kwargs["data"]["episode[published_at]"])
            else:
                payload = kwargs["json"]["episode"]
                self.writes.append(copy.deepcopy(payload))
                self.episode["attributes"].update(payload)
        return Response(200, {"data": self.episode})


class RecoveryTests(unittest.TestCase):
    def test_replanned_remote_draft_recovers_after_uncertain_create(self) -> None:
        local = {
            "video_id": VIDEO_ID, "video_url": YOUTUBE_URL, "base_title": "Title",
            "image_url": "https://example.test/image.jpg", "published_at": "2026-09-01T00:00:00Z",
            "description_text": "Description", "description_sha256": sha256_text("Description"),
            "audio_path": "unused.m4a", "audio_sha256": "unused",
        }
        plan = {
            "youtube_snapshot": {"fresh": True}, "publish_blocked_reasons": [],
            "publish_actions": [{"action": "create_draft_then_publish", "local": local, "remote_precondition": None}],
            "projected_feed": [{"video_id": VIDEO_ID, "planned_publish": True, "target_title": "立正说 001｜Title", "target_number": 1}],
            "projected_reorder_actions": [],
        }
        session = CreatedButResponseLostSession()
        client = client_for(session)
        client.authorize_upload = Mock(return_value={"upload_url": "unused", "audio_url": "unused", "content_type": "audio/mp4"})
        client.upload_audio = Mock()
        with patch.object(executor, "verify_audio", return_value=Path("unused.m4a")):
            with self.assertRaises(AmbiguousMutationError):
                executor.apply_publish(client, "show", plan, Mock())
        self.assertEqual(session.create_count, 1)
        self.assertEqual(session.episode["attributes"]["status"], "draft")
        _, remote = client.episodes_by_video_id("show")
        # A fresh plan targets the same unique draft by semantic YouTube identity.
        plan["publish_actions"][0].update(action="update_draft_then_publish", remote_precondition=planner.compact_episode(remote[VIDEO_ID][0]))
        self.assertEqual(executor.apply_publish(client, "show", plan, Mock()), (1, 1))
        self.assertEqual(session.create_count, 1)
        self.assertEqual(session.episode["attributes"]["status"], "published")
        self.assertTrue(all(payload["youtube_url"] == YOUTUBE_URL for payload in session.writes))
        self.assertTrue(all("video_url" not in payload for payload in session.writes))
        self.assertEqual(client.upload_audio.call_count, 1)


class NumberingSession(CreatedButResponseLostSession):
    def __init__(self, *, reject_number: bool = False, lose_publish_response: bool = False) -> None:
        super().__init__()
        self.reject_number = reject_number
        self.lose_publish_response = lose_publish_response
        self.numbers_at_publish: list[int] = []

    def request(self, method: str, url: str, **kwargs):
        if method == "POST":
            try:
                super().request(method, url, **kwargs)
            except requests.ReadTimeout:
                # Simulate a service with unrelated high-numbered historical drafts.
                self.episode["attributes"]["number"] = 999
                return Response(201, {"data": self.episode})
        if method == "PATCH" and url.endswith("/publish"):
            self.numbers_at_publish.append(self.episode["attributes"]["number"])
            response = super().request(method, url, **kwargs)
            if self.lose_publish_response:
                raise requests.ReadTimeout("published successfully, response lost")
            return response
        response = super().request(method, url, **kwargs)
        if method == "PATCH" and self.reject_number:
            self.episode["attributes"]["number"] = 999
            return Response(200, {"data": self.episode})
        return response


class FrozenNumberTests(unittest.TestCase):
    @staticmethod
    def fixture(*, draft: bool = False, **session_options):
        local = {
            "video_id": VIDEO_ID, "video_url": YOUTUBE_URL, "base_title": "Title",
            "image_url": "https://example.test/image.jpg", "published_at": "2026-09-01T00:00:00Z",
            "description_text": "Description", "description_sha256": sha256_text("Description"),
            "audio_path": "unused.m4a", "audio_sha256": "unused",
        }
        plan = {
            "youtube_snapshot": {"fresh": True}, "publish_blocked_reasons": [],
            "publish_actions": [{
                "action": "update_draft_then_publish" if draft else "create_draft_then_publish",
                "local": local, "remote_precondition": {"episode_id": "one"} if draft else None,
            }],
            "projected_feed": [{"video_id": VIDEO_ID, "planned_publish": True, "target_title": "立正说 001｜Title", "target_number": 533}],
            "projected_reorder_actions": [],
        }
        session = NumberingSession(**session_options)
        if draft:
            session.episode = {"id": "one", "attributes": {
                "status": "draft", "number": 999, "title": "Old draft", "youtube_url": YOUTUBE_URL,
                "media_url": "https://example.test/audio.mp3",
            }}
        client = client_for(session)
        client.authorize_upload = Mock(return_value={"upload_url": "unused", "audio_url": "unused", "content_type": "audio/mp4"})
        client.upload_audio = Mock()
        return plan, session, client

    def test_new_and_repaired_drafts_have_frozen_number_before_publication(self) -> None:
        for draft in (False, True):
            with self.subTest(draft=draft):
                plan, session, client = self.fixture(draft=draft)
                with patch.object(executor, "verify_audio", return_value=Path("unused.m4a")):
                    self.assertEqual(executor.apply_publish(client, "show", plan, Mock()), (1, int(draft)))
                self.assertEqual(session.numbers_at_publish, [533])
                self.assertEqual(session.episode["attributes"]["number"], 533)
                self.assertTrue(all(payload["number"] == 533 for payload in session.writes))
                self.assertTrue(all("increment_number" not in payload for payload in session.writes))

    def test_number_readback_failure_never_publishes(self) -> None:
        plan, session, client = self.fixture(draft=True, reject_number=True)
        with self.assertRaisesRegex(executor.PlanPreconditionError, "number"):
            executor.apply_publish(client, "show", plan, Mock())
        self.assertEqual(session.numbers_at_publish, [])
        self.assertEqual(session.episode["attributes"]["status"], "draft")

    def test_uncertain_publish_leaves_the_frozen_number_for_recovery(self) -> None:
        plan, session, client = self.fixture(draft=True, lose_publish_response=True)
        with self.assertRaises(AmbiguousMutationError):
            executor.apply_publish(client, "show", plan, Mock())
        self.assertEqual(session.numbers_at_publish, [533])
        self.assertEqual(session.episode["attributes"]["status"], "published")
        self.assertEqual(session.episode["attributes"]["number"], 533)

    def test_invalid_or_reused_frozen_numbers_fail_before_remote_access(self) -> None:
        for number in (None, 0, -1, True, "533", 533.0):
            with self.subTest(number=number):
                plan, _, _ = self.fixture()
                plan["projected_feed"][0]["target_number"] = number
                client = Mock()
                with self.assertRaisesRegex(executor.PlanPreconditionError, "number"):
                    executor.apply_publish(client, "show", plan, Mock())
                self.assertEqual(client.mock_calls, [])
        plan, _, _ = self.fixture()
        plan["projected_feed"].append({"video_id": "BBBBBBBBBBB", "planned_publish": False, "target_number": 533})
        client = Mock()
        with self.assertRaisesRegex(executor.PlanPreconditionError, "number"):
            executor.apply_publish(client, "show", plan, Mock())
        self.assertEqual(client.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
