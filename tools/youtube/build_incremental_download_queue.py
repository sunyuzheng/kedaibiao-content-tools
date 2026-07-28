#!/usr/bin/env python3
"""Build a bounded yt-dlp queue from current listing minus known local IDs."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LISTING = PROJECT_ROOT / "tools" / "youtube" / "public_videos_snapshot.json"
DOWNLOAD_ARCHIVE = PROJECT_ROOT / "archive" / "downloaded_history.txt"
DENIED_ARCHIVE = PROJECT_ROOT / "archive" / "public_access_denied.txt"
ARCHIVE_DIR = PROJECT_ROOT / "archive"
CHANNEL_ID = "UC_5lJHgnMP_lb_VpIiXV0hQ"


def two_column_youtube_ids(path: Path) -> set[str]:
    result: set[str] = set()
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "youtube":
            result.add(parts[1])
    return result


def one_column_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    }


def local_member_ids(path: Path) -> set[str]:
    result: set[str] = set()
    if not path.exists():
        return result
    for info_path in path.glob("**/*.info.json"):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (info.get("availability") or "").lower() not in {
            "subscriber_only",
            "premium_only",
            "needs_auth",
        }:
            continue
        if info.get("id"):
            result.add(str(info["id"]))
    return result


def atomic_write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        for line in lines:
            handle.write(line + "\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listing", type=Path, default=LISTING)
    parser.add_argument("--download-archive", type=Path, default=DOWNLOAD_ARCHIVE)
    parser.add_argument("--denied-archive", type=Path, default=DENIED_ARCHIVE)
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-channel-id", default=CHANNEL_ID)
    args = parser.parse_args()

    listing = json.loads(args.listing.read_text(encoding="utf-8"))
    if listing.get("channel_id") != args.expected_channel_id:
        raise RuntimeError("YouTube listing channel id mismatch")
    videos = listing.get("videos") or []
    listing_ids = [item.get("video_id") for item in videos]
    if not listing_ids or any(not video_id for video_id in listing_ids):
        raise RuntimeError("YouTube listing is empty or contains missing ids")
    if len(listing_ids) != len(set(listing_ids)):
        raise RuntimeError("YouTube listing contains duplicate ids")

    downloaded = two_column_youtube_ids(args.download_archive)
    denied = one_column_ids(args.denied_archive)
    known_members = local_member_ids(args.archive_dir)
    skipped = downloaded | denied | known_members
    queued_ids = [video_id for video_id in listing_ids if video_id not in skipped]
    atomic_write_lines(
        args.out,
        [f"https://www.youtube.com/watch?v={video_id}" for video_id in queued_ids],
    )
    print(json.dumps({
        "listing_count": len(listing_ids),
        "downloaded_count": len(downloaded & set(listing_ids)),
        "denied_count": len(denied & set(listing_ids)),
        "known_member_count": len(known_members & set(listing_ids)),
        "queue_count": len(queued_ids),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
