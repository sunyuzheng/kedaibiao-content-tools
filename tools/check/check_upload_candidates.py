#!/usr/bin/env python3
"""Compatibility view of the canonical podcast sync plan.

Unlike the historical implementation, this command scans every archive
location through the canonical manifest semantics and treats existing drafts
as remote state. It never writes to Transistor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.check.build_podcast_sync_plan import build_plan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--max-youtube-snapshot-age-hours",
        type=float,
        default=72,
        help="Fail closed for publication when the YouTube privacy snapshot is older.",
    )
    args = parser.parse_args()

    plan = build_plan(args.max_youtube_snapshot_age_hours)
    blocked_publish = [
        item for item in plan["blocked"] if item.get("scope") == "publish"
    ]
    candidates = [
        {
            "folder_name": Path(item["local"]["folder"]).name,
            "folder": item["local"]["folder"],
            "video_id": item["local"]["video_id"],
            "date": item["local"]["published_at"][:10].replace("-", ""),
            "action": item["action"],
            "transcript_chars": item["local"]["transcript_chars"],
        }
        for item in plan["publish_actions"]
    ]
    output = {
        "count": len(candidates),
        "candidates": candidates,
        "blocked_count": len(blocked_publish),
        "blocked": blocked_publish,
        "youtube_snapshot": plan["youtube_snapshot"],
        "plan_hash": plan["plan_hash"],
        "publish_approval_hash": plan["publish_approval_hash"],
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"可执行发布候选: {len(candidates)}")
        print(f"被前置条件阻止: {len(blocked_publish)}")
        print(
            "YouTube 快照: "
            f"{'fresh' if plan['youtube_snapshot']['fresh'] else 'STALE'} "
            f"({plan['youtube_snapshot']['age_hours']}h)"
        )
        for item in candidates:
            print(
                f"  {item['date']}  {item['action']:<25} "
                f"{item['video_id']}  {item['folder_name']}"
            )
        if blocked_publish:
            print("\nBlocked:")
            for item in blocked_publish:
                print(
                    f"  {item['video_id']}  {','.join(item['reasons'])}  {item['title']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
