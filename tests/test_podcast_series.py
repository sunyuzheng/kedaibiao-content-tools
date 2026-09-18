"""Title migration safety: independently counted series and immutable remote fields."""
import copy
import tempfile
import unittest
from pathlib import Path

from tools.podcast.core import strip_episode_number
from tools.podcast.series import episode_series_title, next_assignment, validate_catalog
from tools.upload.migrate_podcast_series import build_plan, apply_plan, scope_hash

A, B = "AAAAAAAAAAA", "BBBBBBBBBBB"
CATALOG = {
    A: {"series": "solo", "series_number": 1, "global_number": 1,
        "source_date": "20200101", "classification_basis": "Host lecture"},
    B: {"series": "dialogue", "series_number": 1, "global_number": 2,
        "source_date": "20200102", "classification_basis": "Interview"},
}


def remote_fixture():
    return [{"id": str(n), "relationships": {"show": {"data": {"id": "71709"}}},
        "attributes": {"title": f"E{n}. 标题{n}", "number": n, "status": "published",
            "video_url": f"https://www.youtube.com/watch?v={vid}", "published_at": "2020-01-01T00:00:00Z",
            "media_url": "https://example.test/audio.mp3", "description": "Original notes"}}
        for n, vid in enumerate((A, B), 1)]


class MemoryClient:
    def __init__(self, episodes):
        self.data = {e["id"]: copy.deepcopy(e) for e in episodes}
        self.writes = []
        self.damage = False

    def list_episodes(self, show):
        return copy.deepcopy(list(self.data.values()))

    def get_episode(self, eid):
        return copy.deepcopy(self.data[eid])

    def update_episode(self, eid, payload):
        self.writes.append((eid, copy.deepcopy(payload)))
        self.data[eid]["attributes"].update(payload)
        if self.damage:
            self.data[eid]["attributes"]["number"] = 999


class SeriesTests(unittest.TestCase):
    def test_independent_prefixes_preserve_manual_title_body(self):
        self.assertEqual(episode_series_title("E532. Github七万星｜津晶", B, catalog=CATALOG), "对话 001｜Github七万星｜津晶")
        self.assertEqual(episode_series_title("立正说 001｜主题", A, catalog=CATALOG), "立正说 001｜主题")
        self.assertEqual(strip_episode_number("E2. 标题中E2. 不动"), "标题中E2. 不动")

    def test_unknown_and_duplicate_assignments_fail(self):
        with self.assertRaises(ValueError):
            episode_series_title("Title", "CCCCCCCCCCC", catalog=CATALOG)
        bad = copy.deepcopy(CATALOG); bad[B]["series"] = "solo"
        with self.assertRaises(ValueError):
            validate_catalog(bad)

    def test_append_does_not_renumber_existing_or_reuse_other_series_number(self):
        original = copy.deepcopy(CATALOG)
        self.assertEqual(next_assignment(CATALOG, "dialogue", "20260101", "Guest interview")["series_number"], 2)
        self.assertEqual(CATALOG, original)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.remote = remote_fixture()
        self.plan = build_plan(self.remote, "71709", CATALOG)
        self.client = MemoryClient(self.remote)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "ledger.jsonl"

    def run_plan(self):
        return apply_plan(self.client, self.plan, self.plan["approval_hash"], self.ledger)

    def test_only_title_is_sent_and_other_fields_are_unchanged(self):
        result = self.run_plan()
        self.assertEqual(result["count"], 2)
        self.assertTrue(all(set(payload) == {"title"} for _, payload in self.client.writes))
        for old in self.remote:
            new = self.client.data[old["id"]]
            self.assertEqual({k: v for k, v in new["attributes"].items() if k != "title"},
                {k: v for k, v in old["attributes"].items() if k != "title"})

    def test_scope_tampering_and_wrong_approval_do_not_write(self):
        self.plan["actions"][0]["after_title"] = "立正说 001｜Unreviewed rewrite"
        with self.assertRaises(ValueError): self.run_plan()
        self.assertEqual(self.client.writes, [])

    def test_signed_plan_still_cannot_rewrite_title_body(self):
        self.plan["actions"][0]["after_title"] = "立正说 001｜Unreviewed rewrite"
        self.plan["approval_hash"] = scope_hash(self.plan)
        with self.assertRaises(ValueError): self.run_plan()
        self.assertEqual(self.client.writes, [])

    def test_late_episode_drift_blocks_the_entire_batch_before_writes(self):
        self.client.data["2"]["attributes"]["title"] = "New owner edit"
        with self.assertRaises(ValueError): self.run_plan()
        self.assertEqual(self.client.writes, [])

    def test_wrong_show_blocks_before_writes(self):
        self.client.data["2"]["relationships"]["show"]["data"]["id"] = "wrong"
        with self.assertRaises(ValueError): self.run_plan()
        self.assertEqual(self.client.writes, [])

    def test_partial_resume_skips_already_applied_and_is_idempotent(self):
        self.client.data["1"]["attributes"]["title"] = self.plan["actions"][0]["after_title"]
        result = self.run_plan()
        self.assertEqual(result["counts"], {"already_applied": 1, "updated": 1})
        self.assertEqual(len(self.client.writes), 1)
        result = self.run_plan()
        self.assertEqual(result["counts"], {"already_applied": 2})
        self.assertEqual(len(self.client.writes), 1)

    def test_remote_side_effect_fails_readback(self):
        self.client.damage = True
        with self.assertRaises(ValueError): self.run_plan()
        self.assertEqual(len(self.client.writes), 1)


if __name__ == "__main__":
    unittest.main()
