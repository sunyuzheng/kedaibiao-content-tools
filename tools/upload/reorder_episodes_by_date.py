#!/usr/bin/env python3
"""Plan or apply an approval-gated Transistor episode reorder.

The command defaults to a read-only plan. It refuses to create a write plan if
any published episode cannot be mapped to a local YouTube date, because a
partial ordering can silently renumber the whole public feed incorrectly.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.check.build_library_manifest import local_records  # noqa: E402
from tools.podcast.core import (  # noqa: E402
    atomic_write_json,
    canonical_json,
    extract_video_id,
    load_env,
    numbered_title,
    require_transistor_config,
    sha256_text,
    utc_now,
)
from tools.podcast.transistor_client import TransistorClient  # noqa: E402


DEFAULT_OUT_DIR = PROJECT_ROOT / "logs" / "podcast_sync" / "plans" / "reorder"


def build_local_date_map() -> dict[str, str]:
    result: dict[str, str] = {}
    duplicates: set[str] = set()
    for record in local_records():
        video_id = record.get("video_id")
        date = record.get("upload_date")
        if not video_id or not date:
            continue
        if video_id in result and result[video_id] != date:
            duplicates.add(video_id)
        result[video_id] = date
    if duplicates:
        raise RuntimeError(f"Conflicting local dates for video ids: {sorted(duplicates)}")
    return result


def build_reorder_plan(
    client: TransistorClient,
    show_id: str,
) -> dict[str, Any]:
    local_dates = build_local_date_map()
    episodes = [
        episode
        for episode in client.list_episodes(show_id)
        if episode.get("attributes", {}).get("status") == "published"
    ]
    records: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    seen_video_ids: list[str] = []
    for episode in episodes:
        attrs = episode.get("attributes", {})
        video_id = extract_video_id(attrs.get("video_url"))
        date = local_dates.get(video_id or "")
        if not video_id or not date:
            missing.append({
                "episode_id": str(episode.get("id") or ""),
                "video_id": video_id or "",
                "title": attrs.get("title") or "",
            })
            continue
        seen_video_ids.append(video_id)
        records.append({
            "episode_id": str(episode.get("id") or ""),
            "video_id": video_id,
            "date": date,
            "current_number": attrs.get("number"),
            "current_title": attrs.get("title") or "",
            "remote_updated_at": attrs.get("updated_at"),
        })

    duplicates = sorted(
        video_id for video_id, count in Counter(seen_video_ids).items() if count > 1
    )
    blocked_reasons: list[str] = []
    if missing:
        blocked_reasons.append("published_episodes_missing_local_date")
    if duplicates:
        blocked_reasons.append("duplicate_published_video_ids")

    actions: list[dict[str, Any]] = []
    if not blocked_reasons:
        records.sort(key=lambda item: (item["date"], item["video_id"]))
        for target_number, record in enumerate(records, 1):
            target_title = numbered_title(record["current_title"], target_number)
            if (
                record["current_number"] == target_number
                and record["current_title"] == target_title
            ):
                continue
            actions.append({
                **record,
                "target_number": target_number,
                "target_title": target_title,
            })

    approval_scope = {
        "kind": "transistor_reorder",
        "show_id": show_id,
        "items": actions,
    }
    return {
        "schema_version": 1,
        "kind": "transistor_reorder_plan",
        "generated_at": utc_now(),
        "show_id": show_id,
        "published_episode_count": len(episodes),
        "blocked_reasons": blocked_reasons,
        "missing_local_date": missing,
        "duplicate_video_ids": duplicates,
        "actions": actions,
        "approval_hash": sha256_text(canonical_json(approval_scope)),
    }


def apply_plan(
    client: TransistorClient,
    plan: dict[str, Any],
    approval_hash: str | None,
) -> int:
    if plan.get("blocked_reasons"):
        raise RuntimeError(f"Reorder is blocked: {plan['blocked_reasons']}")
    actual_hash = sha256_text(canonical_json({
        "kind": "transistor_reorder",
        "show_id": plan.get("show_id"),
        "items": plan.get("actions", []),
    }))
    if actual_hash != plan.get("approval_hash"):
        raise RuntimeError(
            f"Reorder plan was modified: embedded={plan.get('approval_hash')} actual={actual_hash}"
        )
    if not approval_hash or approval_hash != actual_hash:
        raise RuntimeError(
            f"Exact --approval-hash is required: {actual_hash}"
        )
    updated = 0
    for action in plan["actions"]:
        episode = client.get_episode(action["episode_id"])
        attrs = episode.get("attributes", {})
        observed_video_id = extract_video_id(attrs.get("video_url"))
        precondition = (
            attrs.get("status") == "published"
            and observed_video_id == action["video_id"]
            and attrs.get("number") == action["current_number"]
            and (attrs.get("title") or "") == action["current_title"]
        )
        if not precondition:
            raise RuntimeError(
                f"Remote precondition changed for episode {action['episode_id']}"
            )
        client.update_episode(
            action["episode_id"],
            {
                "number": action["target_number"],
                "title": action["target_title"],
            },
        )
        exact = client.get_episode(action["episode_id"])
        exact_attrs = exact.get("attributes", {})
        if (
            exact_attrs.get("number") != action["target_number"]
            or exact_attrs.get("title") != action["target_title"]
        ):
            raise RuntimeError(
                f"Reorder readback failed for episode {action['episode_id']}"
            )
        updated += 1
        print(
            f"✅ {action['episode_id']}: "
            f"E{action['current_number']} -> E{action['target_number']}"
        )
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="Apply a previously reviewed plan")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-hash")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compatibility alias; planning is already the default.",
    )
    args = parser.parse_args()

    load_env()
    api_key, show_id = require_transistor_config()
    client = TransistorClient(api_key)
    if args.plan:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    else:
        plan = build_reorder_plan(client, show_id)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = args.out_dir / f"reorder-plan-{stamp}.json"
        atomic_write_json(path, plan)
        atomic_write_json(args.out_dir / "latest.json", plan)
        print(f"Plan: {path}")

    if str(plan.get("show_id")) != str(show_id):
        raise RuntimeError("Plan show id does not match configured Transistor show")
    print(f"Published episodes: {plan['published_episode_count']}")
    print(f"Actions: {len(plan['actions'])}")
    print(f"Blocked: {plan['blocked_reasons']}")
    print(f"Approval hash: {plan['approval_hash']}")
    if args.apply:
        updated = apply_plan(client, plan, args.approval_hash)
        print(f"Updated: {updated}")
    return 0 if not plan.get("blocked_reasons") else 2


if __name__ == "__main__":
    raise SystemExit(main())
