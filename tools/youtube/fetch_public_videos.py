#!/usr/bin/env python3
"""Refresh an anonymous, current list of publicly visible channel videos."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_ID = "UC_5lJHgnMP_lb_VpIiXV0hQ"
DEFAULT_OUT = PROJECT_ROOT / "tools" / "youtube" / "public_videos_snapshot.json"
PROJECT_YTDLP = PROJECT_ROOT / ".venv-podcast" / "bin" / "yt-dlp"


def fetch(channel_id: str) -> list[dict[str, Any]]:
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    ytdlp = str(PROJECT_YTDLP) if PROJECT_YTDLP.exists() else "yt-dlp"
    process = subprocess.Popen(
        [
            ytdlp,
            "--flat-playlist",
            "--dump-json",
            "--no-warnings",
            url,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    videos: list[dict[str, Any]] = []
    for line in process.stdout:
        item = json.loads(line)
        observed_channel = item.get("playlist_channel_id") or item.get("playlist_id")
        if observed_channel != channel_id:
            process.kill()
            raise RuntimeError(
                f"Anonymous YouTube channel mismatch: expected={channel_id} "
                f"observed={observed_channel}"
            )
        videos.append({
            "video_id": item.get("id"),
            "title": item.get("title") or "",
            "duration": item.get("duration"),
            "playlist_index": item.get("playlist_index"),
        })
    stderr = process.stderr.read() if process.stderr else ""
    returncode = process.wait()
    if returncode:
        raise RuntimeError(
            f"yt-dlp public listing failed with exit {returncode}: {stderr[-1000:]}"
        )
    ids = [item["video_id"] for item in videos]
    if not videos:
        raise RuntimeError("Anonymous public listing returned zero videos")
    if any(not video_id for video_id in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("Anonymous public listing contains missing or duplicate video ids")
    return videos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", default=CHANNEL_ID)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    videos = fetch(args.channel_id)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "anonymous_youtube_channel_videos",
        "channel_id": args.channel_id,
        "video_count": len(videos),
        "videos": videos,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=args.out.parent,
        prefix=f".{args.out.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(args.out)
    print(
        json.dumps(
            {
                "path": str(args.out),
                "channel_id": args.channel_id,
                "video_count": len(videos),
                "generated_at": payload["generated_at"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
