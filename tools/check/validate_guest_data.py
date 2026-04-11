#!/usr/bin/env python3
"""Validate guests.json consistency for lizheng.ai/guests."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
GUESTS_PATH = ROOT / "guests.json"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)

    if parsed.netloc in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/")[0]

    if parsed.netloc in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [""])[0]
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            return parsed.path.strip("/").split("/")[1]

    return ""


def main() -> int:
    guests = json.loads(GUESTS_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []

    for index, guest in enumerate(guests, start=1):
        guest_name = guest.get("guest_name") or f"guest #{index}"
        all_urls = guest.get("all_urls") or []
        all_video_ids = guest.get("all_video_ids") or []

        derived_ids = []
        for url in all_urls:
            video_id = extract_video_id(url)
            if not video_id:
                errors.append(f"{guest_name}: 无法从 URL 解析 video id -> {url}")
                continue
            if not VIDEO_ID_RE.match(video_id):
                errors.append(f"{guest_name}: 非法 video id -> {video_id} ({url})")
                continue
            derived_ids.append(video_id)

        deduped_ids = list(dict.fromkeys(derived_ids))

        if deduped_ids != all_video_ids:
            errors.append(
                f"{guest_name}: all_video_ids 与 all_urls 不一致 "
                f"(urls={len(deduped_ids)}, ids={len(all_video_ids)})"
            )

        if len(all_video_ids) != len(set(all_video_ids)):
            errors.append(f"{guest_name}: all_video_ids 存在重复")

        if guest.get("episode_count") != len(all_video_ids):
            errors.append(
                f"{guest_name}: episode_count={guest.get('episode_count')} "
                f"但 all_video_ids={len(all_video_ids)}"
            )

        primary_video_id = guest.get("primary_video_id") or ""
        primary_url = guest.get("primary_url") or ""
        primary_url_video_id = extract_video_id(primary_url) if primary_url else ""

        if primary_video_id and primary_video_id not in all_video_ids:
            errors.append(f"{guest_name}: primary_video_id 未包含在 all_video_ids 中")

        if primary_url and primary_url_video_id != primary_video_id:
            errors.append(
                f"{guest_name}: primary_url 与 primary_video_id 不一致 "
                f"({primary_url_video_id} != {primary_video_id})"
            )

    if errors:
        print(f"guest data validation failed: {len(errors)} issue(s)")
        for issue in errors:
            print(f"- {issue}")
        return 1

    total_guests = len(guests)
    total_episodes = sum(len(guest.get("all_video_ids") or []) for guest in guests)
    print(
        f"guest data validation passed: {total_guests} guests, {total_episodes} episodes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
