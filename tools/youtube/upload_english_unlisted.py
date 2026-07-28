#!/usr/bin/env python3
"""Upload selected English relationship videos as unlisted YouTube videos."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from auth import get_youtube_client


TOOL_DIR = Path(__file__).parent
DEFAULT_MANIFEST = TOOL_DIR / "english_unlisted_manifest.json"
DEFAULT_RESULTS = TOOL_DIR / "english_unlisted_upload_results.json"
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def validate_item(item: dict[str, Any]) -> None:
    required = ["slug", "file", "title", "description"]
    missing = [key for key in required if not item.get(key)]
    if missing:
        raise ValueError(f"{item.get('slug', '<unknown>')} missing: {', '.join(missing)}")

    video_path = Path(item["file"])
    if not video_path.exists():
        raise FileNotFoundError(f"{item['slug']}: video file not found: {video_path}")


def upload_video(youtube: Any, item: dict[str, Any]) -> str:
    body = {
        "snippet": {
            "title": item["title"],
            "description": item["description"],
            "tags": item.get("tags", []),
            "categoryId": "28",
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(item["file"], chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"  progress: {int(status.progress() * 100)}%", flush=True)
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS_CODES or retry >= 5:
                raise
            retry += 1
            delay = 2**retry
            print(f"  retryable HTTP {exc.resp.status}; sleeping {delay}s", flush=True)
            time.sleep(delay)

    video_id = response["id"]
    return f"https://www.youtube.com/watch?v={video_id}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--only", action="append", help="Upload only the given slug. Can be repeated.")
    parser.add_argument("--limit", type=int, help="Maximum number of new uploads in this run.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = load_json(args.manifest, [])
    results = load_json(args.results, {})
    only = set(args.only or [])

    selected = []
    for item in manifest:
        validate_item(item)
        if only and item["slug"] not in only:
            continue
        if item["slug"] in results:
            continue
        selected.append(item)

    if args.limit is not None:
        selected = selected[: args.limit]

    print(f"Manifest: {args.manifest}", flush=True)
    print(f"Already uploaded: {len(results)}", flush=True)
    print(f"Selected for this run: {len(selected)}", flush=True)

    if args.dry_run:
        for item in selected:
            print(f"- {item['slug']}: {item['title']} -> {item['file']}", flush=True)
        return

    youtube = get_youtube_client()
    for item in selected:
        print(f"\nUploading {item['slug']}: {item['title']}", flush=True)
        url = upload_video(youtube, item)
        results[item["slug"]] = {
            "url": url,
            "title": item["title"],
            "websiteCard": item.get("websiteCard"),
        }
        save_json(args.results, results)
        print(f"  uploaded: {url}", flush=True)

    print(f"\nDone. Results saved to {args.results}", flush=True)


if __name__ == "__main__":
    main()
