#!/usr/bin/env python3
"""Give published Show Notes the current approved promotion wrapper, after the episode.

Podcast apps preview each episode with the opening of its Show Notes; the v1
wrapper opened every episode with the same host introduction. This moves the
introduction to just above the footer and drops the episode-content heading,
and replaces a wrapper written with a superseded copy (legacy config
previous_promotions) with the current one. Every episode block keeps its stored
bytes (promotion.rewrap_published).

`prepare` reads the live show once and writes a plan, payloads and a review page
under logs/. `execute` reuses the description executor (exact readback, rollback
of the episode on mismatch); without --apply it only runs the read-only preflight,
and --apply requires the exact description approval hash.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.podcast.core import (  # noqa: E402
    atomic_write_json, canonical_json, extract_video_id, load_env, plan_hash,
    require_transistor_config, sha256_file, sha256_text, utc_now,
)
from tools.podcast.promotion import (  # noqa: E402
    CONFIG_FILES, _load_config, promotion_parts, promotion_render_inputs, rewrap_published,
    text_of, blocks_of,
)
from tools.podcast.transistor_client import TransistorClient  # noqa: E402
from tools.upload.apply_podcast_sync_plan import (  # noqa: E402
    Ledger, PlanPreconditionError, apply_descriptions, load_and_verify_plan,
    read_description, verify_approval,
)

SHOW_ID = "71709"
PURPOSE = "show_notes_current_promotion"
PROTECTED = ("title", "status", "number", "season", "published_at", "duration",
             "video_url", "slug", "type", "explicit")


def load_promotion() -> tuple[dict, list[dict]]:
    """The current approved copy and the copies it superseded."""
    promotion, legacy = _load_config(promotion_render_inputs("reorder", ROOT), ROOT)
    return promotion, legacy.get("previous_promotions", [])


def opening(description: str) -> str:
    """What a podcast app shows in an episode list: the first words."""
    words = " ".join(text_of(b) for b in blocks_of(description)[0][:2])
    return words[:60]


def copy_rows(old: dict, new: dict) -> str:
    """The shared wrapper paragraph by paragraph, with changed paragraphs marked."""
    before, after = ([text_of(b) for part in promotion_parts(p) for b in blocks_of(part)[0]]
                     for p in (old, new))
    return "".join(f"<tr{' class=changed' if a != b else ''}><td>{html.escape(a)}</td>"
                   f"<td>{html.escape(b)}</td></tr>" for a, b in zip(before, after))


def build_review(plan: dict, output: Path, promotion: dict, previous: list[dict]) -> None:
    rows = []
    for item in plan["description_actions"]:
        rows.append(f"<tr><td>{html.escape(item['title'])}</td>"
                    f"<td>{html.escape(item['opening_before'])}</td>"
                    f"<td>{html.escape(item['opening_after'])}</td></tr>")
    skipped = "".join(f"<li>{html.escape(s['title'])}：{html.escape(s['reason'])}</li>"
                      for s in plan["skipped"])
    copies = "".join(f"<h2>统一的那几段：{html.escape(p['version'])} → {html.escape(promotion['version'])}</h2>"
                     f"<table><tr><th>改前</th><th>改后</th></tr>{copy_rows(p, promotion)}</table>"
                     for p in previous)
    output.write_text(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>每期简介换成现行模板</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f4f2ec}}
tr.changed td{{background:#fff6d6}}</style>
<h1>每期简介换成现行模板</h1>
<p>{len(plan['description_actions'])}期要改。每期正文原样不动，只动各期统一的那几段（主播介绍、问问立正、社区、课程）：放到本期内容之后，文字用现行模板；每期自己的社区问题保留。</p>
<p>审批哈希：{plan['description_approval_hash']}</p>
{copies}
<p>不改的 {len(plan['skipped'])} 期：</p><ul>{skipped}</ul>
<table><tr><th>单集</th><th>列表里现在露出的开头</th><th>改后露出的开头</th></tr>{''.join(rows)}</table></html>""",
                      encoding="utf-8")


def prepare(args) -> None:
    load_env()
    key, show = require_transistor_config()
    if show != SHOW_ID:
        raise ValueError("Configured show differs from this tool's show")
    promotion, previous = load_promotion()
    out = args.output.resolve()
    out.relative_to(ROOT)  # the executor only reads payloads inside the project
    (out / "payloads").mkdir(parents=True, exist_ok=True)
    episodes = TransistorClient(key).list_episodes(SHOW_ID)
    atomic_write_json(out / "snapshot.json", {"show_id": SHOW_ID, "taken_at": utc_now(), "episodes": episodes})
    actions, skipped = [], []
    for episode in episodes:
        attrs = episode["attributes"]
        if attrs["status"] != "published":
            continue
        episode_id = str(episode["id"])
        before = str(attrs.get("description") or "").strip()
        try:
            after = rewrap_published(before, promotion, previous)
        except ValueError as exc:
            skipped.append({"episode_id": episode_id, "title": attrs["title"], "reason": str(exc)})
            continue
        if after == before:
            skipped.append({"episode_id": episode_id, "title": attrs["title"], "reason": "已经是现行模板"})
            continue
        video_id = extract_video_id(attrs.get("video_url"))
        if not video_id:
            skipped.append({"episode_id": episode_id, "title": attrs["title"], "reason": "没有核实的YouTube来源"})
            continue
        path = out / "payloads" / f"{episode_id}.html"
        path.write_text(after, encoding="utf-8")
        actions.append({
            "episode_id": episode_id, "video_id": video_id, "title": attrs["title"],
            "episode_status": "published",
            "description_path": str(path.relative_to(ROOT)),
            "description_sha256": sha256_text(after),
            "remote_description_sha256": sha256_text(before),
            "metadata": {k: attrs.get(k) for k in PROTECTED},
            "opening_before": opening(before), "opening_after": opening(after),
        })
    plan = {"kind": "kedaibiao_podcast_sync_plan", "purpose": PURPOSE, "show_id": SHOW_ID,
            "generated_at": utc_now(),
            "sources": {relative: sha256_file(ROOT / relative) for relative in CONFIG_FILES.values()},
            "description_actions": actions, "skipped": skipped,
            "publish_actions": [], "transcript_actions": [], "projected_reorder_actions": [],
            "title_actions": []}
    plan["description_approval_hash"] = sha256_text(canonical_json(
        {"kind": "podcast_description_update", "show_id": SHOW_ID, "items": actions}))
    plan["plan_hash"] = plan_hash(plan)
    atomic_write_json(out / "plan.json", plan)
    build_review(plan, out / "review.html", promotion, previous)
    print(json.dumps({"plan": str(out / "plan.json"), "review": str(out / "review.html"),
                      "to_update": len(actions), "skipped": len(skipped),
                      "approval_hash": plan["description_approval_hash"]}, ensure_ascii=False, indent=2))


def execute(args) -> None:
    plan = load_and_verify_plan(args.plan)
    if plan.get("purpose") != PURPOSE or plan.get("show_id") != SHOW_ID:
        raise PlanPreconditionError("Unexpected reorder scope")
    verify_approval(plan, "description", args.approval_hash if args.apply else plan["description_approval_hash"])
    for relative, digest in plan["sources"].items():
        if sha256_file(ROOT / relative) != digest:
            raise PlanPreconditionError(f"Reviewed source changed: {relative}")
    load_env()
    key, show = require_transistor_config()
    if show != SHOW_ID:
        raise PlanPreconditionError("Configured show differs from reviewed show")
    client = TransistorClient(key)
    # Validate every target and file before the first write.
    current = {str(e["id"]): e["attributes"] for e in client.list_episodes(SHOW_ID)}
    preflight = []
    for item in plan["description_actions"]:
        read_description(item["description_path"], item["description_sha256"])
        attrs = current.get(item["episode_id"])
        if attrs is None:
            raise PlanPreconditionError(f"Episode disappeared: {item['episode_id']}")
        if {k: attrs.get(k) for k in PROTECTED} != item["metadata"]:
            raise PlanPreconditionError(f"Protected metadata changed: {item['episode_id']}")
        now = sha256_text(str(attrs.get("description") or "").strip())
        if now not in {item["remote_description_sha256"], item["description_sha256"]}:
            raise PlanPreconditionError(f"Description drift: {item['episode_id']}")
        preflight.append({"episode_id": item["episode_id"], "protected": item["metadata"],
                          "audio_sha256": sha256_text(str(attrs.get("media_url") or "")),
                          "transcript_sha256": sha256_text(canonical_json(attrs.get("transcripts"))),
                          "email_notifications": attrs.get("email_notifications")})
    atomic_write_json(args.plan.parent / "preflight.json", {"plan_hash": plan["plan_hash"], "items": preflight})
    if not args.apply:
        print(f"Read-only preflight passed for {len(preflight)} episodes; no writes.")
        return
    ledger = Ledger(plan, "show-notes-current-promotion")
    updated, already = apply_descriptions(client, plan, ledger)
    after = {str(e["id"]): e["attributes"] for e in client.list_episodes(SHOW_ID)}
    for item in preflight:
        attrs = after[item["episode_id"]]
        if ({k: attrs.get(k) for k in PROTECTED} != item["protected"]
                or sha256_text(str(attrs.get("media_url") or "")) != item["audio_sha256"]
                or sha256_text(canonical_json(attrs.get("transcripts"))) != item["transcript_sha256"]
                or attrs.get("email_notifications") != item["email_notifications"]):
            raise PlanPreconditionError(f"Protected fields changed after reorder: {item['episode_id']}")
    atomic_write_json(args.plan.parent / "receipt.json",
                      {"plan_hash": plan["plan_hash"], "updated": updated, "already_current": already,
                       "verified": len(preflight), "completed_at": utc_now(), "ledger": str(ledger.path)})
    print(f"Updated {updated}, already current {already}; verified {len(preflight)} episodes.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(handler=prepare)
    e = sub.add_parser("execute")
    e.add_argument("--plan", type=Path, required=True)
    e.add_argument("--approval-hash")
    e.add_argument("--apply", action="store_true")
    e.set_defaults(handler=execute)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
