#!/usr/bin/env python3
"""Build tracked guest_video_metadata.json from guests.json + all_videos_full.json."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GUESTS_PATH = ROOT / "guests.json"
ALL_VIDEOS_PATH = ROOT / "tools" / "youtube" / "all_videos_full.json"
OUTPUT_PATH = ROOT / "guest_video_metadata.json"


def main() -> int:
    guests = json.loads(GUESTS_PATH.read_text(encoding="utf-8"))
    all_videos = json.loads(ALL_VIDEOS_PATH.read_text(encoding="utf-8"))
    all_videos_by_id = {video["video_id"]: video for video in all_videos}

    ordered_ids: list[str] = []
    seen: set[str] = set()
    missing_ids: list[str] = []

    for guest in guests:
        for video_id in guest.get("all_video_ids") or []:
            if video_id in seen:
                continue
            seen.add(video_id)
            ordered_ids.append(video_id)
            if video_id not in all_videos_by_id:
                missing_ids.append(video_id)

    if missing_ids:
        raise SystemExit(
            "Missing metadata in all_videos_full.json for: "
            + ", ".join(sorted(missing_ids))
        )

    output = [
        {
            "video_id": video_id,
            "title": all_videos_by_id[video_id].get("title", ""),
            "published_at": all_videos_by_id[video_id].get("published_at", ""),
            "view_count": all_videos_by_id[video_id].get("view_count", 0),
        }
        for video_id in ordered_ids
    ]

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote {len(output)} guest video metadata rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
