"""Cloud failure boundaries: fresh runners, partial writes, private state and exact artifacts."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.automation import cloud_podcast as cloud
from tools.podcast import cloud_state as storage
from tools.podcast.core import atomic_write_json, sha256_file, sha256_text

A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"
SHA = "a" * 40


def info(video_id=A):
    return {"id": video_id, "title": "A public episode", "upload_date": "20260920", "availability": "public",
            "live_status": "not_live", "channel_id": storage.CHANNEL_ID}


def state():
    return {"schema_version": 1, "show_id": storage.SHOW_ID, "channel_id": storage.CHANNEL_ID,
            "records": {A: info(A)}, "pending_execution": None}


def episode(video_id=A, status="published"):
    return {"id": "1", "attributes": {"youtube_url": f"https://youtube.com/watch?v={video_id}", "status": status}}


class SelectionTests(unittest.TestCase):
    def test_only_ahead_of_latest_published_never_historical_gap(self):
        videos = [{"video_id": x, "playlist_index": n} for n, x in enumerate((B, A, C), 1)]
        self.assertEqual([r["video_id"] for r in cloud.select_candidates(videos, [episode()])], [B])

    def test_empty_remote_baseline_fails_closed(self):
        with self.assertRaises(storage.CloudBlocked):
            cloud.select_candidates([{"video_id": A, "playlist_index": 1}], [])

    def test_ambiguous_remote_identity_fails_before_download(self):
        videos = [{"video_id": A, "playlist_index": 1}]
        for episodes in ([episode(), episode()], [{"id": "1", "attributes": {"status": "published"}}]):
            with self.assertRaises(storage.CloudBlocked):
                cloud.select_candidates(videos, episodes)

    def test_unbounded_backlog_is_not_downloaded(self):
        videos = [{"video_id": f"{i:011}", "playlist_index": i + 1} for i in range(12)]
        with self.assertRaises(storage.CloudBlocked):
            cloud.select_candidates(videos, [episode("00000000011")])

    def test_members_live_and_unknown_visibility(self):
        self.assertEqual(cloud.verify_probe(A, info()), "public")
        self.assertEqual(cloud.verify_probe(A, {**info(), "availability": "subscriber_only"}), "excluded_member")
        self.assertEqual(cloud.verify_probe(A, {**info(), "was_live": True}), "excluded_live")
        for change in ({"availability": None}, {"availability": "unlisted"}, {"channel_id": "wrong"}, {"id": B}):
            with self.assertRaises(storage.CloudBlocked):
                cloud.verify_probe(A, {**info(), **change})

    def test_plan_default_and_incomplete_standing_grant(self):
        self.assertFalse(cloud.standing_policy({"enabled": False}))
        with self.assertRaises(storage.CloudBlocked):
            cloud.standing_policy({"enabled": True})
        valid = {"enabled": True, "schema_version": 1, "show_id": storage.SHOW_ID,
                 "channel_id": storage.CHANNEL_ID, "max_items": 3, "scope": "new_public_normal_only",
                 "local_writer_disabled": True, "approved_by": "owner", "approved_at": "2026-09-30"}
        self.assertTrue(cloud.standing_policy(valid))
        for key in ("local_writer_disabled", "approved_by", "scope"):
            invalid = dict(valid); invalid.pop(key)
            with self.assertRaises(storage.CloudBlocked):
                cloud.standing_policy(invalid)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "app"; self.root.mkdir()
        self.catalog = self.base / "catalog.json"; self.catalog.write_text('{}')
        self.bundle = self.base / "bundle"
        self.destination = self.base / "restore"; self.destination.mkdir()
        atomic_write_json(self.root / storage.PLAN_PATH,
                          {"plan_hash": "p" * 64, "publish_approval_hash": "h" * 64, "publish_actions": []})
        for name in storage.SNAPSHOTS:
            atomic_write_json(self.root / name, {})
        storage.restore_metadata(self.root, state())

    def export(self):
        return storage.export_bundle(self.root, self.bundle, code_sha=SHA, catalog=self.catalog)

    def restore(self, **kwargs):
        args = dict(code_sha=SHA, catalog=self.catalog, plan_hash="p" * 64, approval_hash="h" * 64)
        args.update(kwargs)
        return storage.import_bundle(self.bundle, self.destination, **args)

    def test_minimum_history_contains_no_signed_urls_or_media(self):
        original = {**info(), "formats": [{"url": "private-signed-url"}], "http_headers": {"Cookie": "secret"},
                    "subtitles": {"en": [{"url": "token"}]}, "description": "not needed in historical index"}
        self.assertEqual(storage.minimal_info(original), info())
        files = list((self.root / "archive").rglob("*"))
        self.assertEqual(sum(p.is_file() for p in files), 1)

    def test_round_trip_restores_historical_dates_without_audio(self):
        self.export(); self.restore()
        restored = storage.folder_for(self.destination, info()) / "source.info.json"
        self.assertEqual(json.loads(restored.read_text()), info())

    def test_tampered_audio_rejected_before_copy(self):
        audio = storage.folder_for(self.root, info()) / "source.m4a"; audio.write_bytes(b"audio")
        self.export()
        (storage.folder_for(self.bundle, info()) / "source.m4a").write_bytes(b"evil!")
        with self.assertRaises(storage.CloudBlocked):
            self.restore()
        self.assertFalse((self.destination / storage.PLAN_PATH).exists())

    def test_code_catalog_and_scope_changes_rejected(self):
        self.export()
        for kwargs in ({"code_sha": "b" * 40}, {"approval_hash": "other"}, {"plan_hash": "other"}):
            with self.assertRaises(storage.CloudBlocked):
                self.restore(**kwargs)
        self.catalog.write_text('{"changed":true}')
        with self.assertRaises(storage.CloudBlocked):
            self.restore()

    def test_executable_secret_and_traversal_paths_rejected(self):
        for name in (".env", "../outside", "/tmp/outside", "tools/automation/cloud_podcast.py",
                     "archive/无人工字幕/20260920_AAAAAAAAAAA/../../evil.py"):
            self.assertFalse(storage.allowed_bundle_path(name))
        manifest = self.export()
        manifest["files"]["../outside"] = {"sha256": "x", "bytes": 1}
        atomic_write_json(self.bundle / "manifest.json", manifest)
        with self.assertRaises(storage.CloudBlocked):
            self.restore()

    def test_parent_symlink_rejected(self):
        self.export()
        (self.destination / "archive").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(storage.CloudBlocked):
            self.restore()


class ReconciliationTests(unittest.TestCase):
    def test_unknown_classification_saves_dot_task_without_downloading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); current = state(); receipt = {}
            rows = [{"video_id": B, "playlist_index": 1}, {"video_id": A, "playlist_index": 2}]
            with patch.object(cloud, "ROOT", root), patch("tools.youtube.fetch_public_videos.fetch", return_value=rows), \
                 patch.object(cloud, "probe", return_value=info(B)), patch.object(cloud, "load_catalog", return_value={}), \
                 patch.object(cloud, "download") as download:
                result = cloud.build_cloud_plan(current, root, [episode()], receipt)
            self.assertIsNone(result)
            download.assert_not_called()
            self.assertEqual(current["dot_actions"][0]["video_id"], B)
            self.assertEqual(receipt["status"], "needs_classification")

    def test_unpublished_candidate_redownloaded_despite_existing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); current = state(); current["records"][B] = info(B)
            rows = [{"video_id": B, "playlist_index": 1}, {"video_id": A, "playlist_index": 2}]
            with patch.object(cloud, "ROOT", root), patch("tools.youtube.fetch_public_videos.fetch", return_value=rows), \
                 patch.object(cloud, "probe", return_value=info(B)), patch.object(cloud, "load_catalog", return_value={B: {}}), \
                 patch.object(cloud, "download") as download, \
                 patch("tools.check.build_podcast_sync_plan.build_plan", return_value={"plan_hash": "x"}):
                cloud.build_cloud_plan(current, root, [episode()], {})
            download.assert_called_once()

    def test_four_eligible_items_download_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); current = state()
            videos = [f"{i:011}" for i in range(4)] + [A]
            rows = [{"video_id": v, "playlist_index": n} for n, v in enumerate(videos, 1)]
            with patch.object(cloud, "ROOT", root), patch("tools.youtube.fetch_public_videos.fetch", return_value=rows), \
                 patch.object(cloud, "probe", side_effect=info), patch.object(cloud, "load_catalog", return_value={v: {} for v in videos}), \
                 patch.object(cloud, "download") as download:
                with self.assertRaises(storage.CloudBlocked):
                    cloud.build_cloud_plan(current, root, [episode()], {})
            download.assert_not_called()

    def test_uncertain_published_response_is_verified_not_republished(self):
        current = state()
        target = {"title": "对话 001｜Topic", "number": 1, "description_sha256": sha256_text("notes"),
                  "published_at": "2026-09-20T00:00:00Z"}
        current["pending_execution"] = {"plan_hash": "original", "targets": {A: target}}
        remote = episode(); remote["attributes"].update(title=target["title"], number=1, description="notes", published_at=target["published_at"])
        with patch.object(cloud, "stream_runner") as checks, patch.object(cloud, "json_runner", return_value={"action_count": 0}):
            cloud.recover_pending(current, [remote])
        self.assertIsNone(current["pending_execution"])
        self.assertEqual(current["last_recovery"]["published_verified"], [A])
        self.assertIn("check_upload_quality.py", checks.call_args.args[0][1])

    def test_uncertain_publication_mismatch_keeps_pending(self):
        current = state(); current["pending_execution"] = {"targets": {A: {"title": "expected"}}}
        with self.assertRaises(storage.CloudBlocked):
            cloud.recover_pending(current, [episode()])
        self.assertIsNotNone(current["pending_execution"])

    def test_local_execution_cannot_cross_publication_checkpoint(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "false"}):
            with self.assertRaises(storage.CloudBlocked):
                cloud.checkpoint(Path("/unused"), state())

class RecoveryAndProbeRegressionTests(unittest.TestCase):
    def test_member_error_is_excluded_but_bot_check_is_not(self):
        import subprocess
        member = subprocess.CompletedProcess([], 1, "", f"ERROR: [youtube] {A}: Join this channel to get access to members-only content")
        bot = subprocess.CompletedProcess([], 1, "", f"ERROR: [youtube] {A}: Sign in to confirm you're not a bot")
        with patch.object(cloud.subprocess, "run", return_value=member):
            self.assertEqual(cloud.verify_probe(A, cloud.probe(A)), "excluded_member")
        with patch.object(cloud.subprocess, "run", return_value=bot):
            with self.assertRaises(storage.CloudBlocked):
                cloud.probe(A)

    def test_partial_batch_retains_unresolved_target(self):
        current = state()
        target = {"title": "对话 001｜Topic", "number": 1, "description_sha256": sha256_text("notes"),
                  "published_at": "2026-09-20T00:00:00Z"}
        current["pending_execution"] = {"plan_hash": "original", "targets": {A: target, B: target}}
        remote = episode(); remote["attributes"].update(title=target["title"], number=1, description="notes", published_at=target["published_at"])
        with patch.object(cloud, "stream_runner"), patch.object(cloud, "json_runner", return_value={"action_count": 0}):
            cloud.recover_pending(current, [remote])
        self.assertIsNotNone(current["pending_execution"])
        self.assertEqual(current["last_recovery"]["remaining"], [B])

    def test_unfinished_target_behind_baseline_is_never_silently_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            current = state(); current["pending_execution"] = {"targets": {B: {}}}
            current["last_recovery"] = {"remaining": [B]}
            rows = [{"video_id": A, "playlist_index": 1}, {"video_id": B, "playlist_index": 2}]
            with patch.object(cloud, "ROOT", Path(directory)), patch("tools.youtube.fetch_public_videos.fetch", return_value=rows):
                with self.assertRaises(storage.CloudBlocked):
                    cloud.build_cloud_plan(current, Path(directory), [episode()], {})

    def test_exclusion_after_plan_prevents_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = Path(directory)
            atomic_write_json(ops / "excluded-videos.json", {"videos": {A: {"basis": "Internal demo"}}})
            with patch.object(cloud, "probe") as probe:
                with self.assertRaises(storage.CloudBlocked):
                    cloud.validate_live_candidates({"publish_actions": [{"local": {"video_id": A}}]}, ops)
            probe.assert_not_called()

class LegacyDraftQuarantineTests(unittest.TestCase):
    def test_unrelated_old_drafts_do_not_stop_incremental_discovery(self):
        rows = [{"video_id": B, "playlist_index": 1}, {"video_id": A, "playlist_index": 2}]
        remote = [episode(), episode(C, "draft"), episode(C, "draft"), {"id": "orphan", "attributes": {"status": "draft"}}]
        self.assertEqual([r["video_id"] for r in cloud.select_candidates(rows, remote)], [B])

    def test_duplicate_current_candidate_drafts_still_block(self):
        rows = [{"video_id": B, "playlist_index": 1}, {"video_id": A, "playlist_index": 2}]
        with self.assertRaises(storage.CloudBlocked):
            cloud.select_candidates(rows, [episode(), episode(B, "draft"), episode(B, "draft")])

class ExecutionBoundaryTests(unittest.TestCase):
    def exercise(self, mode, checkpoint_error=None):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / 'app'; ops = base / 'ops'; root.mkdir(); ops.mkdir()
            atomic_write_json(ops / 'runtime/state.json', state())
            atomic_write_json(ops / 'podcast_series.json', {'schema_version': 1, 'episodes': {}})
            (ops / 'code-version.txt').write_text(SHA)
            atomic_write_json(ops / 'publication-policy.json', {
                'enabled': True, 'schema_version': 1, 'show_id': storage.SHOW_ID,
                'channel_id': storage.CHANNEL_ID, 'scope': 'new_public_normal_only', 'max_items': 3,
                'local_writer_disabled': True, 'approved_by': 'owner', 'approved_at': '2026-09-30'})
            args = Namespace(ops=ops, mode=mode, bundle=None, plan_hash='', approval_hash='')
            plan = {'plan_hash': 'p', 'publish_approval_hash': 'a'}
            with patch.object(cloud, 'ROOT', root), patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}), \
                 patch.object(cloud, 'require_transistor_config', return_value=('test-key', storage.SHOW_ID)), \
                 patch.object(cloud.subprocess, 'check_output', return_value=SHA), \
                 patch.object(cloud, 'TransistorClient') as client, \
                 patch.object(cloud, 'build_cloud_plan', return_value=plan), \
                 patch.object(cloud, 'evaluate_auto_publish', return_value={'eligible': True, 'has_candidates': True, 'blockers': []}), \
                 patch.object(cloud, 'export_bundle'), patch.object(cloud, 'validate_live_candidates'), \
                 patch.object(cloud, 'pending_targets', return_value={'targets': {B: {}}, 'plan_hash': 'p'}), \
                 patch.object(cloud, 'checkpoint', side_effect=checkpoint_error) as checkpoint, \
                 patch('tools.automation.sync_podcast.execute_auto_publish') as execute:
                client.return_value.list_episodes.return_value = [episode()]
                result = cloud.run(args)
                saved = json.loads((ops / 'runtime/state.json').read_text())
                return result, saved, checkpoint.call_count, execute.call_count

    def test_plan_never_executes_even_with_enabled_standing_policy(self):
        result, saved, checkpoints, executions = self.exercise('plan')
        self.assertEqual((result, checkpoints, executions), (0, 0, 0))
        self.assertEqual(saved['latest_receipt']['status'], 'awaiting_approval')

    def test_failed_durable_checkpoint_prevents_remote_publication(self):
        result, saved, checkpoints, executions = self.exercise('auto', storage.CloudBlocked('state unavailable'))
        self.assertEqual((result, checkpoints, executions), (1, 1, 0))
        self.assertEqual(saved['pending_execution']['plan_hash'], 'p')
        self.assertEqual(saved['latest_receipt']['status'], 'failed')

class ApprovalAndContinuityRegressionTests(unittest.TestCase):
    def test_manifest_approval_cannot_cover_a_different_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / 'source'; bundle = base / 'bundle'; target = base / 'target'
            source.mkdir(); target.mkdir(); catalog = base / 'catalog'; catalog.write_text('{}')
            atomic_write_json(source / storage.PLAN_PATH, {'plan_hash': 'reviewed', 'publish_approval_hash': 'approved', 'publish_actions': []})
            for name in storage.SNAPSHOTS:
                atomic_write_json(source / name, {})
            manifest = storage.export_bundle(source, bundle, code_sha=SHA, catalog=catalog)
            atomic_write_json(bundle / storage.PLAN_PATH, {'plan_hash': 'different-plan', 'publish_approval_hash': 'different-scope'})
            path = bundle / storage.PLAN_PATH
            manifest['files'][storage.PLAN_PATH] = {'bytes': path.stat().st_size, 'sha256': sha256_file(path)}
            atomic_write_json(bundle / 'manifest.json', manifest)
            with self.assertRaises(storage.CloudBlocked):
                storage.import_bundle(bundle, target, code_sha=SHA, catalog=catalog, plan_hash='reviewed', approval_hash='approved')
            self.assertFalse((target / storage.PLAN_PATH).exists())

    def test_another_approved_plan_cannot_erase_unfinished_targets(self):
        current = state(); current['pending_execution'] = {'targets': {A: {}, B: {}}}
        current['last_recovery'] = {'remaining': [B]}
        with self.assertRaises(storage.CloudBlocked):
            cloud.verify_pending_coverage(current, {'publish_actions': [{'local': {'video_id': C}}]})
        cloud.verify_pending_coverage(current, {'publish_actions': [{'local': {'video_id': B}}]})
        self.assertIsNotNone(current['pending_execution'])

    def test_unfinished_target_becoming_member_is_a_visible_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            current = state(); current['pending_execution'] = {'targets': {B: {}}}; current['last_recovery'] = {'remaining': [B]}
            rows = [{'video_id': B, 'playlist_index': 1}, {'video_id': A, 'playlist_index': 2}]
            with patch.object(cloud, 'ROOT', Path(directory)), patch('tools.youtube.fetch_public_videos.fetch', return_value=rows), \
                 patch.object(cloud, 'load_catalog', return_value={B: {}}), \
                 patch.object(cloud, 'probe', return_value={**info(B), 'availability': 'subscriber_only'}):
                with self.assertRaises(storage.CloudBlocked):
                    cloud.build_cloud_plan(current, Path(directory), [episode()], {})
            self.assertIsNotNone(current['pending_execution'])


if __name__ == "__main__":
    unittest.main()
