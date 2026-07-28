#!/usr/bin/env python3
"""Build a read-only, approval-ready plan for duplicate Transistor drafts.

The public Transistor API does not expose episode deletion. This tool therefore
only identifies exact dashboard cleanup targets and preserves one canonical
draft per YouTube video_id.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.podcast.core import (  # noqa: E402
    atomic_write_json,
    canonical_json,
    extract_video_id,
    load_env,
    require_transistor_config,
    sha256_text,
    utc_now,
)
from tools.podcast.transistor_client import TransistorClient  # noqa: E402


DEFAULT_SYNC_PLAN = PROJECT_ROOT / "logs" / "podcast_sync" / "plans" / "latest.json"
DEFAULT_OUT_DIR = PROJECT_ROOT / "logs" / "podcast_sync" / "draft_cleanup"


def compact_draft(episode: dict[str, Any]) -> dict[str, Any]:
    attrs = episode.get("attributes", {})
    return {
        "episode_id": str(episode.get("id") or ""),
        "video_id": extract_video_id(attrs.get("video_url")),
        "status": attrs.get("status"),
        "title": attrs.get("title"),
        "number": attrs.get("number"),
        "created_at": attrs.get("created_at"),
        "updated_at": attrs.get("updated_at"),
        "media_url_present": bool(attrs.get("media_url")),
        "description_chars": len(attrs.get("description") or ""),
        "image_url_present": bool(attrs.get("image_url")),
        "transcript_present": bool(
            attrs.get("transcript_text")
            or attrs.get("transcript_url")
            or attrs.get("transcripts")
        ),
    }


def timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def choose_canonical(drafts: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the newest draft; metadata can be deterministically repaired later."""
    if not drafts:
        raise ValueError("Cannot choose a canonical draft from an empty group")

    def rank(item: dict[str, Any]) -> tuple[float, int, float, int]:
        completeness = sum(
            (
                bool(item.get("media_url_present")),
                bool(item.get("description_chars")),
                bool(item.get("image_url_present")),
                bool(item.get("transcript_present")),
            )
        )
        episode_id = int(item["episode_id"]) if item["episode_id"].isdigit() else 0
        return (
            timestamp(item.get("created_at")),
            completeness,
            timestamp(item.get("updated_at")),
            episode_id,
        )

    return max(drafts, key=rank)


def build_cleanup_plan(
    sync_plan: dict[str, Any],
    episodes: list[dict[str, Any]],
) -> dict[str, Any]:
    if sync_plan.get("kind") != "kedaibiao_podcast_sync_plan":
        raise RuntimeError("Source is not a podcast sync plan")
    show_id = str(sync_plan.get("show_id") or "")
    if not show_id:
        raise RuntimeError("Source plan has no Transistor show id")

    draft_by_video: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        draft = compact_draft(episode)
        if draft["status"] == "draft" and draft["video_id"]:
            draft_by_video[draft["video_id"]].append(draft)

    multiple_ids = sorted(
        item["video_id"]
        for item in sync_plan.get("blocked", [])
        if item.get("scope") == "publish"
        and "multiple_remote_drafts" in item.get("reasons", [])
    )
    unverified_ids = sorted(
        item["video_id"]
        for item in sync_plan.get("blocked", [])
        if item.get("scope") == "publish"
        and "youtube_candidate_not_verified" in item.get("reasons", [])
    )

    groups: list[dict[str, Any]] = []
    deletion_actions: list[dict[str, Any]] = []
    for video_id in multiple_ids:
        drafts = sorted(
            draft_by_video.get(video_id, []),
            key=lambda item: int(item["episode_id"]),
        )
        if len(drafts) < 2:
            raise RuntimeError(
                f"Remote precondition changed for {video_id}: "
                f"expected multiple drafts, found {len(drafts)}"
            )
        canonical = choose_canonical(drafts)
        extras = [
            item for item in drafts if item["episode_id"] != canonical["episode_id"]
        ]
        groups.append(
            {
                "video_id": video_id,
                "identity_proof": "same_youtube_video_id",
                "canonical_draft": canonical,
                "duplicate_drafts": extras,
            }
        )
        for duplicate in extras:
            deletion_actions.append(
                {
                    "action": "delete_duplicate_draft_in_dashboard",
                    "episode_id": duplicate["episode_id"],
                    "video_id": video_id,
                    "keep_episode_id": canonical["episode_id"],
                    "remote_precondition": duplicate,
                }
            )

    quarantined: list[dict[str, Any]] = []
    for video_id in unverified_ids:
        if video_id in multiple_ids:
            continue
        drafts = sorted(
            draft_by_video.get(video_id, []),
            key=lambda item: int(item["episode_id"]),
        )
        quarantined.append(
            {
                "video_id": video_id,
                "recommendation": (
                    "keep_unique_draft_quarantined"
                    if len(drafts) == 1
                    else "no_remote_draft_to_clean"
                    if not drafts
                    else "unexpected_multiple_drafts"
                ),
                "drafts": drafts,
                "reason": (
                    "youtube_verification_failure_can_be_transient; "
                    "do_not_delete_the_only_remote_copy"
                ),
            }
        )

    cleanup_scope = {
        "kind": "transistor_duplicate_draft_cleanup",
        "show_id": show_id,
        "source_plan_hash": sync_plan.get("plan_hash"),
        "items": deletion_actions,
    }
    return {
        "schema_version": 1,
        "kind": "transistor_draft_cleanup_plan",
        "generated_at": utc_now(),
        "show_id": show_id,
        "source_plan_hash": sync_plan.get("plan_hash"),
        "duplicate_groups": groups,
        "deletion_actions": deletion_actions,
        "quarantined_unverified": quarantined,
        "cleanup_approval_hash": sha256_text(canonical_json(cleanup_scope)),
    }


def write_markdown(path: Path, plan: dict[str, Any]) -> None:
    lines = [
        "# Transistor draft cleanup plan",
        "",
        f"- Generated: `{plan['generated_at']}`",
        f"- Show: `{plan['show_id']}`",
        f"- Source sync plan: `{plan['source_plan_hash']}`",
        f"- Duplicate groups: `{len(plan['duplicate_groups'])}`",
        f"- Dashboard deletion targets: `{len(plan['deletion_actions'])}`",
        f"- Unique unverified candidates kept: `{len(plan['quarantined_unverified'])}`",
        f"- Cleanup approval hash: `{plan['cleanup_approval_hash']}`",
        "",
        "This is a read-only plan. Transistor's public API does not expose episode "
        "deletion, so any deletion must be performed in the dashboard after exact review.",
        "",
        "## Duplicate groups",
        "",
        "| video_id | keep episode | delete episodes | evidence |",
        "|---|---:|---|---|",
    ]
    for group in plan["duplicate_groups"]:
        delete_ids = ", ".join(
            f"`{item['episode_id']}`" for item in group["duplicate_drafts"]
        )
        lines.append(
            f"| `{group['video_id']}` | "
            f"`{group['canonical_draft']['episode_id']}` | {delete_ids} | "
            f"{group['identity_proof']} |"
        )
    lines += [
        "",
        "## Unique but unverified",
        "",
        "| video_id | recommendation | draft episode ids |",
        "|---|---|---|",
    ]
    for item in plan["quarantined_unverified"]:
        draft_ids = ", ".join(
            f"`{draft['episode_id']}`" for draft in item["drafts"]
        ) or "none"
        lines.append(
            f"| `{item['video_id']}` | {item['recommendation']} | {draft_ids} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync-plan", type=Path, default=DEFAULT_SYNC_PLAN)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    sync_plan = json.loads(args.sync_plan.read_text(encoding="utf-8"))
    load_env()
    api_key, configured_show_id = require_transistor_config()
    if str(sync_plan.get("show_id")) != configured_show_id:
        raise RuntimeError("Configured show id does not match source sync plan")
    episodes = TransistorClient(api_key).list_episodes(configured_show_id)
    plan = build_cleanup_plan(sync_plan, episodes)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"draft-cleanup-{stamp}.json"
    md_path = args.out_dir / f"draft-cleanup-{stamp}.md"
    atomic_write_json(json_path, plan)
    write_markdown(md_path, plan)
    atomic_write_json(args.out_dir / "latest.json", plan)
    write_markdown(args.out_dir / "latest.md", plan)

    summary = {
        "plan_path": str(json_path),
        "summary_path": str(md_path),
        "duplicate_group_count": len(plan["duplicate_groups"]),
        "deletion_count": len(plan["deletion_actions"]),
        "quarantined_unverified_count": len(plan["quarantined_unverified"]),
        "cleanup_approval_hash": plan["cleanup_approval_hash"],
    }
    print(
        json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if args.json
        else "\n".join(f"{key}: {value}" for key, value in summary.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
