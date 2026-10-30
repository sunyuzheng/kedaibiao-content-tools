"""Approved payload bytes, immutable inputs, and publication boundary checks."""
from __future__ import annotations

import copy
import itertools
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
    CONFIG_FILES, CONTENT_FIRST_HTML_FORMAT, EPISODE_HEADING, PROMOTION_HTML_FORMAT, blocks_of,
    promotion_parts, promotion_render_inputs, refresh, render_promoted_show_notes_html,
    rewrap_published, text_of,
)
from tools.podcast.show_notes import PORTABLE_HTML_FORMAT, render_portable_show_notes_html
from tools.upload import apply_podcast_sync_plan as executor


SOURCE = (
    "节目正文：AI如何改变工作。\n\n章节\n\n00:00 — 开场\n01:00 — 问题"
    "\n\n相关内容\n\nhttps://example.test/resource\n\n{{video}}\n\n{{transcript}}"
)
# Captured from the separately reviewed historical-refresh reference renderer
# (the 2026-10-02 copy, now kept only in the legacy config's previous_promotions).
APPROVED_SAMPLE_SHA256 = "31c7066c85901b3af8f73eb476837529d66cb47fa74d74a5a03a2955b1c1cb0f"
# The same sample and copy in the content-first layout (approved 2026-10-30),
# as published to every wrapped episode that day.
APPROVED_CONTENT_FIRST_SHA256 = "76d54d719a493bf67700bfbea4458224f1fb9bad6585a25baa6673173b7e6e32"
# The same sample content-first with the copy new plans use: the 超线性学院
# outward line (approved 2026-10-30).
APPROVED_CURRENT_SHA256 = "45ff104c8f8107e79750e3c952f444073e3de464f8047c0ee5e016219ac986f5"
# Every YouTube description carries these lines from 2026-10-30
# (lizheng-video-production COMMUNITY_FOOTER); the podcast footer replaces them.
VIDEO_COMMUNITY_LINES = (
    "不怕AI天天变，就来超线性学院。学点真本事，做点真东西。\n"
    "免费加入：https://www.superlinear.academy/ （也可以在App Store搜「超线性学院」）"
)


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
        self.promotion = json.loads((self.root / CONFIG_FILES["promotion"]).read_text())
        self.legacy = json.loads((self.root / CONFIG_FILES["legacy"]).read_text())
        self.previous = self.legacy["previous_promotions"][0]

    def previous_copy(self, source: str = SOURCE, content_first: bool = True) -> str:
        """A description as published with the superseded 2026-10-02 copy."""
        return refresh(source, "Episode", self.previous, self.legacy, content_first)["description"]

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
                self.assertEqual(payload["description_format"], CONTENT_FIRST_HTML_FORMAT)
                expected_kind = "podcast_sidecar" if source_kind == "versioned" else source_kind
                self.assertEqual(payload["description_source"], expected_kind)
                self.assertEqual(payload["description_sha256"], APPROVED_CURRENT_SHA256)
                description = self.execute_description(payload)
                self.assertEqual(sha256_text(description), APPROVED_CURRENT_SHA256)
                # Podcast apps preview the opening: the episode's own words come first.
                self.assertTrue(description.startswith("<p>节目正文：AI如何改变工作。</p>"))
                self.assertNotIn(EPISODE_HEADING, description)
                self.assertEqual(_description_text(payload, self.root)[0], description)
                self.assertEqual(_verify_description(payload, self.root, "AAAAAAAAAAA")[0], [])
                self.assertIn("00:00 — 开场", description)
                self.assertIn("https://example.test/resource", description)
                self.assertIn("{{video}}", description)
                self.assertIn("{{transcript}}", description)

    def test_already_composed_html_remains_byte_identical(self) -> None:
        rendered = render_promoted_show_notes_html(SOURCE, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT)
        payload = self.payload(rendered)
        self.assertEqual(self.execute_description(payload), rendered)
        self.assertEqual(payload["description_sha256"], APPROVED_CURRENT_SHA256)
        v1 = render_promoted_show_notes_html(SOURCE, self.inputs, self.root)
        self.assertEqual(render_promoted_show_notes_html(v1, self.inputs, self.root), v1)
        # The superseded copy kept for recognition still reproduces, byte for byte,
        # what was approved and published with it in both layouts.
        self.assertEqual(sha256_text(self.previous_copy(content_first=False)), APPROVED_SAMPLE_SHA256)
        self.assertEqual(sha256_text(self.previous_copy()), APPROVED_CONTENT_FIRST_SHA256)

    def test_new_episode_uses_the_outward_line_copy(self) -> None:
        description = self.execute_description(self.payload())
        self.assertIn("<p><strong>不怕AI天天变，就来超线性学院</strong></p>\n\n"
                      "<p>学点真本事，做点真东西。超线性学院是我创办的免费AI社区，两万多人在这里分享真实项目，"
                      "你也可以带着问题来讨论。App Store搜「超线性学院」也能加入。</p>\n\n"
                      '<p><a href="https://www.superlinear.academy/">https://www.superlinear.academy/</a></p>',
                      description)
        self.assertIn("《真本事》作者、超线性学院创始人。", description)
        for superseded in ("Superlinear Academy｜免费AI社区", "Superlinear Academy创始人", "Superlinear Academy免费社区"):
            self.assertNotIn(superseded, description)

    def test_video_description_community_line_is_not_repeated(self) -> None:
        # The video description puts the line just above its chapters.
        source = SOURCE.replace("\n\n章节\n\n", "\n\n" + VIDEO_COMMUNITY_LINES + "\n\n章节\n\n")
        self.assertEqual(self.execute_description(self.payload(source)),
                         self.execute_description(self.payload()))
        joined = SOURCE + "\n\n相关链接\n" + VIDEO_COMMUNITY_LINES
        description = self.execute_description(self.payload(joined))
        self.assertEqual(description.count("不怕AI天天变"), 1)
        self.assertNotIn("免费加入：", description)
        self.assertIn("相关链接", description)

    def test_superseded_copy_refreshes_to_the_current_copy(self) -> None:
        question = "你怎样核对AI生成的答案？"
        with_question = SOURCE + "\n\n加入Superlinear Academy免费社区\n\n" + question + "\nhttps://www.superlinear.academy/"
        for source in (SOURCE, with_question):
            current = render_promoted_show_notes_html(source, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT)
            for content_first in (True, False):
                published = self.previous_copy(source, content_first)
                with self.subTest(question=source != SOURCE, content_first=content_first):
                    self.assertEqual(self.execute_description(self.payload(published)), current)
                    # As plain text every paragraph is its own block; only spacing differs.
                    plain = "\n\n".join(text_of(block) for block in blocks_of(published)[0])
                    self.assertEqual(blocks_of(self.execute_description(self.payload(plain)))[0],
                                     blocks_of(current)[0])
            self.assertNotIn(self.previous["community_heading"], current)
            self.assertEqual(current.count(question), 1 if source != SOURCE else 0)

    def test_published_v1_wrapper_moves_without_touching_the_episode(self) -> None:
        v1 = render_promoted_show_notes_html(SOURCE, self.inputs, self.root)
        v2 = render_promoted_show_notes_html(SOURCE, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT)
        self.assertEqual(render_promoted_show_notes_html(v1, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT), v2)
        self.assertEqual(render_promoted_show_notes_html(v2, self.inputs, self.root), v1)
        stored_episode = v1.split(EPISODE_HEADING + "\n\n", 1)[1].split("\n\n<p><strong>问问立正", 1)[0]
        self.assertTrue(v2.startswith(stored_episode + "\n\n<p>我是孙煜征"))
        self.assertEqual(sorted(blocks_of(v1)[0]), sorted(blocks_of(v2)[0] + [EPISODE_HEADING]))

    def test_published_episode_keeps_its_stored_html_when_the_wrapper_moves(self) -> None:
        promotion = self.promotion
        # Published bodies can hold HTML the portable renderer never makes; it stays as stored.
        episode = '<p>正文第一句<br>第二行 <a href="https://example.test/x">链接</a></p>\n<p>章节：</p>'
        for cta in ("", "你怎样在项目中核对AI的答案？"):
            with self.subTest(cta=cta):
                intro, footer = promotion_parts(promotion, cta)
                v1 = intro + "\n\n" + EPISODE_HEADING + "\n\n" + episode + "\n\n" + footer
                moved = rewrap_published(v1, promotion)
                self.assertTrue(moved.startswith(episode + "\n\n<p>我是孙煜征"))
                self.assertTrue(moved.endswith(footer))
                self.assertNotIn(EPISODE_HEADING, moved)
                self.assertEqual(sorted(blocks_of(moved)[0]),
                                 sorted(b for b in blocks_of(v1)[0] if b != EPISODE_HEADING))
                self.assertEqual(rewrap_published(moved, promotion), moved)
        for unwrapped in (episode, "节目正文", intro + "\n\n" + episode):
            with self.subTest(unwrapped=unwrapped[:12]), self.assertRaises(ValueError):
                rewrap_published(unwrapped, promotion)

    def test_published_superseded_copy_gets_the_current_copy_and_keeps_the_episode(self) -> None:
        promotion, previous = self.promotion, [self.previous]
        episode = '<p>正文第一句<br>第二行 <a href="https://example.test/x">链接</a></p>\n<p>章节：</p>'
        for cta in ("", "你怎样在项目中核对AI的答案？"):
            old_intro, old_footer = promotion_parts(self.previous, cta)
            intro, footer = promotion_parts(promotion, cta)
            current = episode + "\n\n" + intro + "\n\n" + footer
            published = {"content_first": episode + "\n\n" + old_intro + "\n\n" + old_footer,
                         "v1": old_intro + "\n\n" + EPISODE_HEADING + "\n\n" + episode + "\n\n" + old_footer}
            for layout, description in published.items():
                with self.subTest(cta=cta, layout=layout):
                    self.assertEqual(rewrap_published(description, promotion, previous), current)
                    # Recognition depends on the registered superseded copy.
                    with self.assertRaisesRegex(ValueError, "No approved promotion wrapper"):
                        rewrap_published(description, promotion)
            self.assertEqual(rewrap_published(current, promotion, previous), current)
            self.assertNotIn(self.previous["community"], current)
            if cta:
                self.assertEqual(current.count(cta), 1)

    def test_introduction_left_inside_the_episode_fails_closed(self) -> None:
        for intro in (self.promotion["intro"], self.previous["intro"]):
            for fmt in (PROMOTION_HTML_FORMAT, CONTENT_FIRST_HTML_FORMAT):
                with self.subTest(fmt=fmt, current=intro == self.promotion["intro"]), \
                        self.assertRaisesRegex(ValueError, "incomplete approved promotion wrapper"):
                    render_promoted_show_notes_html(SOURCE + "\n\n" + intro, self.inputs, self.root, fmt)

    def test_already_composed_plaintext_has_one_wrapper(self) -> None:
        promotion = json.loads((self.root / CONFIG_FILES["promotion"]).read_text())
        for fmt in (PROMOTION_HTML_FORMAT, CONTENT_FIRST_HTML_FORMAT):
            with self.subTest(fmt=fmt):
                rendered = render_promoted_show_notes_html(SOURCE, self.inputs, self.root, fmt)
                plain = "\n\n".join(text_of(block) for block in blocks_of(rendered)[0])
                output = self.execute_description(self.payload(plain))
                self.assertEqual(output.count(promotion["intro"]), 1)
                self.assertEqual(output.count(promotion["ask_heading"]), 1)
                self.assertEqual(output.count("<strong>本期内容</strong>"), 0)

    def test_owned_batch_plaintext_wrapper_is_recognized(self) -> None:
        current = self.promotion
        for p, cta in itertools.product((current, self.previous), ("", "你怎样在项目中核对AI的答案？")):
            sections = [p["intro"], p["personal_label"] + "\n" + p["personal_url"],
                        "本期内容", SOURCE]
            for name in ("ask", "community", "course"):
                text = p[name] + ("\n\n" + cta if name == "community" and cta else "")
                sections.append(p[name + "_heading"] + "\n\n" + text + "\n" + p[name + "_url"])
            output = self.execute_description(self.payload("\n\n".join(sections)))
            self.assertEqual(output.count(current["intro"]), 1)
            self.assertEqual(output.count(current["community_heading"]), 1)
            self.assertEqual(output.count("<strong>本期内容</strong>"), 0)
            if p is self.previous:
                self.assertNotIn(p["intro"], output)
                self.assertNotIn(p["community_heading"], output)
            if cta:
                self.assertEqual(output.count(cta), 1)
            self.assertEqual(render_promoted_show_notes_html(
                output, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT), output)

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
        self.assertEqual(render_promoted_show_notes_html(
            combined, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT), combined)

    def test_legacy_community_preserves_episode_question(self) -> None:
        question = "你怎样核对AI生成的答案？"
        for separator in ("\n", "\n\n"):
            source = SOURCE + "\n\n加入Superlinear Academy免费社区\n\n" + question + separator + "https://www.superlinear.academy/"
            output = self.execute_description(self.payload(source))
            self.assertEqual(output.count(question), 1)
            self.assertGreater(output.index(question), output.index(self.promotion["community_heading"]))
            self.assertEqual(render_promoted_show_notes_html(
                output, self.inputs, self.root, CONTENT_FIRST_HTML_FORMAT), output)

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
        self.assertEqual(payload["description_format"], CONTENT_FIRST_HTML_FORMAT)
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
