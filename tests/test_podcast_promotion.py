"""Approved payload bytes, immutable inputs, and publication boundary checks."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.check import build_podcast_sync_plan as planner
from tools.automation.podcast_auto_publish import _description_text, _verify_description
from tools.podcast.core import PROJECT_ROOT, plan_hash, sha256_text
from tools.podcast.promotion import (
    CONFIG_FILES, PROMOTION_HTML_FORMAT, blocks_of, promotion_render_inputs,
    render_promoted_show_notes_html, text_of,
)
from tools.podcast.show_notes import PORTABLE_HTML_FORMAT, render_portable_show_notes_html
from tools.upload import apply_podcast_sync_plan as executor


SOURCE = (
    "节目正文：AI如何改变工作。\n\n章节\n\n00:00 — 开场\n01:00 — 问题"
    "\n\n相关内容\n\nhttps://example.test/resource\n\n{{video}}\n\n{{transcript}}"
)
# Captured from the separately reviewed historical-refresh reference renderer.
APPROVED_SAMPLE_SHA256 = "31c7066c85901b3af8f73eb476837529d66cb47fa74d74a5a03a2955b1c1cb0f"


class PromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in CONFIG_FILES.values():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT_ROOT / relative, target)
        self.folder = self.root / "archive" / "episode"
        self.folder.mkdir(parents=True)
        (self.folder / "episode.m4a").write_bytes(b"test-audio")
        self.record = {"video_id": "AAAAAAAAAAA", "folder": "archive/episode", "title": "Episode",
                       "upload_date": "20261001", "transcript_status": "missing"}
        self.inputs = promotion_render_inputs("Episode", self.root)

    def payload(self, source: str = SOURCE, *, include_promotion: bool = True) -> dict:
        with mock.patch.object(planner, "PROJECT_ROOT", self.root), mock.patch.object(
            planner, "relative_to_project",
            side_effect=lambda path: str(path.relative_to(self.root)) if path else None,
        ):
            payload, _ = planner.local_payload(self.record, {"description": source},
                                               include_promotion=include_promotion)
        return payload

    def execute_description(self, payload: dict) -> str:
        with mock.patch.object(executor, "PROJECT_ROOT", self.root), mock.patch.object(
            executor, "resolve_project_path",
            side_effect=lambda value: self.root / value if value else None,
        ):
            return executor.desired_episode_fields(payload)["description"]

    def test_fallback_planner_executor_and_gate_reproduce_reviewed_bytes(self) -> None:
        for source_kind in ("youtube_snapshot", "local_youtube_file", "podcast_sidecar", "versioned"):
            with self.subTest(source_kind=source_kind):
                for old in self.folder.glob("*.txt"):
                    old.unlink()
                (self.folder / "episode.description").unlink(missing_ok=True)
                versioned = self.root / "podcast_show_notes" / "AAAAAAAAAAA.txt"
                versioned.unlink(missing_ok=True)
                if source_kind == "local_youtube_file":
                    (self.folder / "episode.description").write_text(SOURCE, encoding="utf-8")
                elif source_kind == "podcast_sidecar":
                    (self.folder / "episode.podcast-description.txt").write_text(SOURCE, encoding="utf-8")
                elif source_kind == "versioned":
                    versioned.write_text(SOURCE, encoding="utf-8")
                    (self.folder / "episode.podcast-description.txt").write_text("lower priority", encoding="utf-8")
                payload = self.payload()
                self.assertEqual(payload["description_format"], PROMOTION_HTML_FORMAT)
                expected_kind = "podcast_sidecar" if source_kind == "versioned" else source_kind
                self.assertEqual(payload["description_source"], expected_kind)
                self.assertEqual(payload["description_sha256"], APPROVED_SAMPLE_SHA256)
                description = self.execute_description(payload)
                self.assertEqual(sha256_text(description), APPROVED_SAMPLE_SHA256)
                self.assertEqual(_description_text(payload, self.root)[0], description)
                self.assertEqual(_verify_description(payload, self.root, "AAAAAAAAAAA")[0], [])
                self.assertIn("00:00 — 开场", description)
                self.assertIn("https://example.test/resource", description)
                self.assertIn("{{video}}", description)
                self.assertIn("{{transcript}}", description)

    def test_already_composed_html_remains_byte_identical(self) -> None:
        rendered = render_promoted_show_notes_html(SOURCE, self.inputs, self.root)
        payload = self.payload(rendered)
        self.assertEqual(self.execute_description(payload), rendered)
        self.assertEqual(payload["description_sha256"], APPROVED_SAMPLE_SHA256)

    def test_already_composed_plaintext_has_one_wrapper(self) -> None:
        rendered = render_promoted_show_notes_html(SOURCE, self.inputs, self.root)
        plain = "\n\n".join(text_of(block) for block in blocks_of(rendered)[0])
        output = self.execute_description(self.payload(plain))
        promotion = json.loads((self.root / CONFIG_FILES["promotion"]).read_text())
        self.assertEqual(output.count(promotion["intro"]), 1)
        self.assertEqual(output.count(promotion["ask_heading"]), 1)
        self.assertEqual(output.count("<strong>本期内容</strong>"), 1)

    def test_owned_batch_plaintext_wrapper_is_recognized(self) -> None:
        p = json.loads((self.root / CONFIG_FILES["promotion"]).read_text())
        for cta in ("", "你怎样在项目中核对AI的答案？"):
            sections = [p["intro"], p["personal_label"] + "\n" + p["personal_url"],
                        "本期内容", SOURCE]
            for name in ("ask", "community", "course"):
                text = p[name] + ("\n\n" + cta if name == "community" and cta else "")
                sections.append(p[name + "_heading"] + "\n\n" + text + "\n" + p[name + "_url"])
            output = self.execute_description(self.payload("\n\n".join(sections)))
            self.assertEqual(output.count(p["intro"]), 1)
            self.assertEqual(output.count(p["ask_heading"]), 1)
            self.assertEqual(output.count("<strong>本期内容</strong>"), 1)
            if cta:
                self.assertEqual(output.count(cta), 1)
            self.assertEqual(render_promoted_show_notes_html(output, self.inputs, self.root), output)

    def test_exact_legacy_is_removed_and_episode_resources_stay(self) -> None:
        source = (SOURCE + "\n\n会员专属社区：www.superlinear.academy\n"
                  "本期合作披露：独立采访，产品 https://example.test/product\n"
                  "课程离线版：https://www.superlinear.academy/c/ai/\n\n"
                  "AI Builders课程：\n\nhttps://ai-builders.com/")
        output = self.execute_description(self.payload(source))
        self.assertNotIn("会员专属社区", output)
        self.assertNotIn("课程离线版", output)
        self.assertNotIn("AI Builders课程：", output)
        self.assertIn("本期合作披露", output)
        self.assertIn("https://example.test/product", output)
        self.assertEqual(output.count('href="https://ai-builders.com/"'), 1)
        # Similar wording is episode material, never a fuzzy deletion match.
        self.assertIn("这次采访讨论旧课程", self.execute_description(self.payload(
            SOURCE + "\n\n这次采访讨论旧课程：https://www.superlinear.academy/c/ai/")))
        combined = self.execute_description(self.payload(SOURCE + "\n\nAI Builders课程：\nhttps://ai-builders.com/"))
        self.assertNotIn("AI Builders课程：", combined)
        self.assertEqual(render_promoted_show_notes_html(combined, self.inputs, self.root), combined)

    def test_legacy_community_preserves_episode_question(self) -> None:
        question = "你怎样核对AI生成的答案？"
        for separator in ("\n", "\n\n"):
            source = SOURCE + "\n\n加入Superlinear Academy免费社区\n\n" + question + separator + "https://www.superlinear.academy/"
            output = self.execute_description(self.payload(source))
            self.assertEqual(output.count(question), 1)
            self.assertGreater(output.index(question), output.index("Superlinear Academy｜免费AI社区"))
            self.assertEqual(render_promoted_show_notes_html(output, self.inputs, self.root), output)

    def test_promotion_only_source_uses_locked_episode_title(self) -> None:
        legacy = json.loads((self.root / CONFIG_FILES["legacy"]).read_text())
        payload = self.payload(legacy["blocks"][2]["text"])
        self.assertIn("<p>Episode</p>", self.execute_description(payload))
        payload["description_renderer_inputs"]["title"] = "Changed title"
        with self.assertRaises(executor.PlanPreconditionError):
            self.execute_description(payload)

    def test_missing_and_changed_config_block_planner_gate_and_executor(self) -> None:
        payload = self.payload()
        for name, relative in CONFIG_FILES.items():
            target = self.root / relative
            original = target.read_bytes()
            for change in ("missing", "changed"):
                with self.subTest(config=name, change=change):
                    if change == "missing":
                        target.unlink()
                    else:
                        target.write_bytes(original + b"\n")
                    with self.assertRaises(executor.PlanPreconditionError):
                        self.execute_description(payload)
                    blockers, _ = _verify_description(payload, self.root, "AAAAAAAAAAA")
                    self.assertEqual(blockers[0]["code"], "description_artifact_invalid")
                    invalid = self.payload()
                    self.assertEqual(invalid["description_chars"], 0)
                    self.assertTrue(invalid["description_quality"]["errors"])
                    target.write_bytes(original)

    def test_missing_source_or_renderer_inputs_and_output_drift_fail_closed(self) -> None:
        payload = self.payload()
        for key in ("description_renderer_inputs", "description_source_sha256"):
            changed = copy.deepcopy(payload)
            changed.pop(key)
            with self.assertRaises(executor.PlanPreconditionError):
                self.execute_description(changed)
        for key in ("description_sha256", "description_source_sha256"):
            changed = copy.deepcopy(payload)
            changed[key] = "0" * 64
            with self.assertRaises(executor.PlanPreconditionError):
                self.execute_description(changed)
        changed = copy.deepcopy(payload)
        changed["description_renderer_inputs"]["config_sha256"]["promotion"] = "0" * 64
        with self.assertRaises(executor.PlanPreconditionError):
            self.execute_description(changed)

    def test_sidecar_mutation_or_disappearance_blocks_gate_and_executor(self) -> None:
        source = self.folder / "episode.podcast-description.txt"
        source.write_text(SOURCE, encoding="utf-8")
        payload = self.payload()
        for mutation in ("changed", "missing"):
            if mutation == "changed":
                source.write_text("Unreviewed edit", encoding="utf-8")
            else:
                source.unlink()
            with self.assertRaises(executor.PlanPreconditionError):
                self.execute_description(payload)
            blockers, _ = _verify_description(payload, self.root, "AAAAAAAAAAA")
            self.assertEqual(blockers[0]["code"], "description_artifact_invalid")

    def test_invalid_source_stays_blocked_and_v1_empty_source_stays_empty(self) -> None:
        for source in ("{{unknown}}", "{{video", "01:00 — later\n00:00 — earlier", "<p>arbitrary HTML</p>", "字" * 9900):
            payload = self.payload(source)
            self.assertEqual(payload["description_chars"], 0)
            self.assertTrue(payload["description_quality"]["errors"])
            with self.assertRaises((executor.PlanPreconditionError, ValueError)):
                self.execute_description(payload)
        payload = self.payload("", include_promotion=False)
        self.assertIsNone(payload["description_format"])
        self.assertEqual(self.execute_description(payload), "")

    def test_empty_new_source_locks_hash_and_roundtrips_with_title_and_warning(self) -> None:
        payload = self.payload("")
        self.assertEqual(payload["description_text"], "")
        self.assertEqual(payload["description_source"], "empty")
        self.assertEqual(payload["description_format"], PROMOTION_HTML_FORMAT)
        self.assertEqual(payload["description_source_sha256"], sha256_text(""))
        self.assertEqual(payload["description_source_chars"], 0)
        self.assertEqual(payload["description_quality"]["errors"], [])
        self.assertIn("empty_source", payload["description_quality"]["warnings"])
        description = self.execute_description(payload)
        self.assertIn("<p>Episode</p>", description)
        self.assertIn("问问立正｜把问题想明白", description)
        self.assertEqual(sha256_text(description), payload["description_sha256"])
        self.assertEqual(_description_text(payload, self.root), (description, ""))
        blockers, receipts = _verify_description(payload, self.root, "AAAAAAAAAAA")
        self.assertEqual(blockers, [])
        self.assertIn("empty_source", {receipt["warning"] for receipt in receipts})
        changed = copy.deepcopy(payload)
        changed["description_text"] = None
        with self.assertRaises(executor.PlanPreconditionError):
            self.execute_description(changed)
        self.assertTrue(_verify_description(changed, self.root, "AAAAAAAAAAA")[0])

    def test_empty_new_source_still_rejects_config_drift(self) -> None:
        payload = self.payload("")
        target = self.root / CONFIG_FILES["promotion"]
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaises(executor.PlanPreconditionError):
            self.execute_description(payload)
        blockers, _ = _verify_description(payload, self.root, "AAAAAAAAAAA")
        self.assertEqual(blockers[0]["code"], "description_artifact_invalid")
        invalid = self.payload("")
        self.assertEqual(invalid["description_chars"], 0)
        self.assertTrue(invalid["description_quality"]["errors"])

    def test_approved_wrapper_does_not_allow_arbitrary_html(self) -> None:
        rendered = render_promoted_show_notes_html(SOURCE, self.inputs, self.root)
        unsafe = rendered.replace("<p>节目正文：AI如何改变工作。</p>", "<p><script>unsafe</script></p>")
        with self.assertRaisesRegex(ValueError, "nonportable episode HTML"):
            render_promoted_show_notes_html(unsafe, self.inputs, self.root)

    def test_frozen_v1_plan_still_executes_without_promotion_config(self) -> None:
        payload = self.payload(include_promotion=False)
        # This old-plan shape has no renderer-input field and no config hash.
        payload.pop("description_renderer_inputs")
        plan = {"kind": "kedaibiao_podcast_sync_plan", "publish_actions": [{"local": payload}]}
        plan["plan_hash"] = plan_hash(plan)
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        for relative in CONFIG_FILES.values():
            (self.root / relative).unlink()
        frozen = executor.load_and_verify_plan(path)
        self.assertEqual(frozen, plan)
        self.assertEqual(payload["description_format"], PORTABLE_HTML_FORMAT)
        self.assertEqual(self.execute_description(frozen["publish_actions"][0]["local"]),
                         render_portable_show_notes_html(SOURCE))
        self.assertEqual(_description_text(payload, self.root)[0], render_portable_show_notes_html(SOURCE))


if __name__ == "__main__":
    unittest.main()
