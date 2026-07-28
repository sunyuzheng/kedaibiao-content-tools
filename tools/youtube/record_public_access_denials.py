#!/usr/bin/env python3
"""Persist only clearly classified member-only yt-dlp failures."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LISTING = PROJECT_ROOT / "tools" / "youtube" / "public_videos_snapshot.json"
DENIED_ARCHIVE = PROJECT_ROOT / "archive" / "public_access_denied.txt"
ERROR_RE = re.compile(r"^ERROR: \[youtube\] ([^:]+): (.*)$")
MEMBER_RE = re.compile(
    r"members?.only|join this channel|subscriber.only|"
    r"available to this channel.s members",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--listing", type=Path, default=LISTING)
    parser.add_argument("--denied-archive", type=Path, default=DENIED_ARCHIVE)
    args = parser.parse_args()

    listing = json.loads(args.listing.read_text(encoding="utf-8"))
    listing_ids = {
        item.get("video_id")
        for item in listing.get("videos", [])
        if item.get("video_id")
    }
    observed_member_ids: set[str] = set()
    unexpected: list[dict[str, str]] = []
    for line in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = ERROR_RE.match(line)
        if not match:
            continue
        video_id, message = match.groups()
        if MEMBER_RE.search(message):
            if video_id not in listing_ids:
                raise RuntimeError(f"Denied id is outside current listing: {video_id}")
            observed_member_ids.add(video_id)
        else:
            unexpected.append({"video_id": video_id, "message": message})

    existing = set()
    if args.denied_archive.exists():
        existing = {
            line.strip()
            for line in args.denied_archive.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        }
    merged = sorted(existing | observed_member_ids)
    args.denied_archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=args.denied_archive.parent,
        prefix=f".{args.denied_archive.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        for video_id in merged:
            handle.write(video_id + "\n")
        temp_path = Path(handle.name)
    temp_path.replace(args.denied_archive)
    print(json.dumps({
        "new_member_denials": len(observed_member_ids - existing),
        "total_member_denials": len(merged),
        "unexpected_errors": unexpected,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
