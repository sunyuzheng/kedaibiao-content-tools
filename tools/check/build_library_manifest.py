#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a canonical local media-library manifest.

This is the audit source of truth. Directory names are physical storage only;
semantic status is derived from info.json, local files, the YouTube snapshot,
and Transistor.

Outputs:
  logs/library_manifest/library_manifest.json
  logs/library_manifest/library_audit.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = PROJECT_ROOT / "archive"
YOUTUBE_SNAPSHOT = PROJECT_ROOT / "tools" / "youtube" / "all_videos_full.json"
OUT_DIR = PROJECT_ROOT / "logs" / "library_manifest"
VIDEO_DIRS = ("有人工字幕", "无人工字幕", "会员视频")
TARGET_SUB_LANG_PREFIXES = ("zh", "en")


def load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def video_id_from_name(name: str) -> str | None:
    if len(name) < 11:
        return None
    candidate = name[-11:]
    if re.match(r"^[A-Za-z0-9_-]{11}$", candidate):
        return candidate
    return None


def target_langs(subtitles: dict[str, Any]) -> list[str]:
    return sorted(
        lang for lang in subtitles
        if lang.startswith(TARGET_SUB_LANG_PREFIXES)
    )


def classify_transcript(info: dict[str, Any], files: list[Path]) -> tuple[str, list[str]]:
    files = [f for f in files if f.exists() and f.stat().st_size > 20]
    manual_langs = target_langs(info.get("subtitles") or {})
    auto_langs = target_langs(info.get("automatic_captions") or {})
    names = [str(f) for f in files]
    base_names = [f.name for f in files]
    has_final = any(name.endswith(".final.srt") for name in base_names)
    has_corrected = any(name.endswith(".corrected.srt") for name in names)
    has_qwen = any(name.endswith(".qwen.srt") for name in names)
    timed = [
        name for name in base_names
        if name.endswith((".srt", ".vtt"))
    ]

    if manual_langs:
        return "human", manual_langs
    if has_corrected or has_final:
        return "local_corrected", []
    if has_qwen:
        return "local_qwen_uncorrected", []
    if auto_langs and timed:
        return "downloaded_auto", auto_langs
    if timed:
        return "timed_unknown", []
    return "missing", []


def load_youtube_snapshot() -> dict[str, dict[str, Any]]:
    if not YOUTUBE_SNAPSHOT.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in read_json(YOUTUBE_SNAPSHOT):
        result.setdefault(item.get("video_id", ""), item)
    result.pop("", None)
    return result


def fetch_transistor_map() -> dict[str, list[dict[str, Any]]]:
    api_key = os.environ.get("TRANSISTOR_API_KEY")
    show_id = os.environ.get("TRANSISTOR_SHOW_ID")
    if not api_key or not show_id:
        return {}

    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    page = 1
    headers = {"x-api-key": api_key}
    while True:
        for attempt in range(4):
            resp = requests.get(
                "https://api.transistor.fm/v1/episodes",
                headers=headers,
                params={
                    "show_id": show_id,
                    "pagination[page]": page,
                    "pagination[per]": 50,
                },
                timeout=30,
            )
            if resp.status_code != 429:
                break
            wait = 60 * (attempt + 1)
            print(f"Transistor rate limit, waiting {wait}s for page {page}...")
            time.sleep(wait)
        resp.raise_for_status()
        data = resp.json()
        for ep in data.get("data", []):
            attrs = ep.get("attributes", {})
            url = attrs.get("video_url") or ""
            vid = None
            if "v=" in url:
                vid = url.split("v=")[-1].split("&")[0].strip()
            elif "youtu.be/" in url:
                vid = url.split("youtu.be/")[-1].split("?")[0].strip()
            if not vid:
                continue
            result[vid].append({
                "episode_id": ep.get("id"),
                "status": attrs.get("status"),
                "number": attrs.get("number"),
                "title": attrs.get("title"),
                "published_at": attrs.get("published_at"),
            })
        meta = data.get("meta", {})
        if page >= meta.get("totalPages", 1):
            break
        page += 1
        time.sleep(1.2)
    return dict(result)


def local_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for category in VIDEO_DIRS:
        base = ARCHIVE_DIR / category
        if not base.exists():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir():
                continue
            info_files = list(folder.glob("*.info.json"))
            info: dict[str, Any] = {}
            if info_files:
                try:
                    info = read_json(info_files[0])
                except Exception as exc:
                    records.append({
                        "video_id": video_id_from_name(folder.name),
                        "folder": str(folder.relative_to(PROJECT_ROOT)),
                        "folder_category": category,
                        "local_status": "bad_info_json",
                        "error": str(exc),
                    })
                    continue

            top_files = [f for f in folder.iterdir() if f.is_file()]
            process_files = [
                f for process_dir in folder.glob("*_process")
                if process_dir.is_dir()
                for f in process_dir.iterdir()
                if f.is_file()
            ]
            files = top_files + process_files
            video_id = info.get("id") or info.get("display_id") or video_id_from_name(folder.name)
            transcript_status, transcript_langs = classify_transcript(info, files)
            audio_files = [
                f.name for f in top_files
                if f.suffix.lower() in {".m4a", ".mp3", ".wav", ".aac", ".opus", ".mp4", ".mov"}
                and ".part" not in f.name
            ]
            subtitle_files = [
                f.name for f in top_files
                if f.name.endswith((".srt", ".vtt"))
            ]
            process_subtitle_files = [
                str(f.relative_to(folder)) for f in process_files
                if f.name.endswith((".srt", ".vtt"))
            ]

            records.append({
                "video_id": video_id,
                "title": info.get("title") or folder.name,
                "upload_date": info.get("upload_date") or (folder.name[:8] if folder.name[:8].isdigit() else ""),
                "folder": str(folder.relative_to(PROJECT_ROOT)),
                "folder_category": category,
                "local_status": "ok" if video_id and audio_files and info_files else "incomplete",
                "availability_in_info": info.get("availability"),
                "live_status_in_info": info.get("live_status"),
                "was_live_in_info": bool(info.get("was_live")),
                "is_live_in_info": bool(info.get("is_live")),
                "content_class": classify_content(info),
                "has_audio": bool(audio_files),
                "audio_files": audio_files,
                "has_info_json": bool(info_files),
                "has_description": bool(list(folder.glob("*.description"))),
                "has_thumbnail": bool([f for f in files if f.suffix.lower() in {".webp", ".jpg", ".jpeg", ".png"}]),
                "subtitle_files": subtitle_files,
                "process_subtitle_files": process_subtitle_files,
                "transcript_status": transcript_status,
                "transcript_langs": transcript_langs,
            })
    return records


def classify_content(info: dict[str, Any]) -> str:
    availability = (info.get("availability") or "").lower()
    live_status = (info.get("live_status") or "").lower()
    if availability in {"subscriber_only", "premium_only", "needs_auth"}:
        return "member_only"
    if bool(info.get("was_live")) or live_status in {"was_live", "is_live", "post_live"}:
        return "live_replay"
    return "normal_video"


def add_remote_and_actions(
    records: list[dict[str, Any]],
    youtube: dict[str, dict[str, Any]],
    transistor: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    for rec in records:
        vid = rec.get("video_id")
        yt = youtube.get(vid or "", {})
        eps = transistor.get(vid or "", [])
        published = [ep for ep in eps if ep.get("status") == "published"]
        drafts = [ep for ep in eps if ep.get("status") == "draft"]

        rec["youtube_privacy"] = yt.get("privacy") or "not_in_current_uploads"
        rec["youtube_published_at"] = yt.get("published_at")
        rec["transistor_status"] = (
            "published" if published else
            "draft" if drafts else
            "absent"
        )
        rec["transistor_published_count"] = len(published)
        rec["transistor_draft_count"] = len(drafts)
        rec["transistor_episode_numbers"] = [
            ep.get("number") for ep in published + drafts
        ]

        rec["podcast_policy"] = classify_podcast_policy(rec)
        rec["action_needed"] = classify_action(rec)
    return records


def classify_podcast_policy(rec: dict[str, Any]) -> str:
    if rec.get("youtube_privacy") != "public":
        return "not_public"
    if rec.get("content_class") == "member_only":
        return "not_podcast_member_only"
    if rec.get("content_class") == "live_replay":
        return "not_podcast_live_replay"
    return "ready_public_normal"


def classify_action(rec: dict[str, Any]) -> str:
    if rec.get("local_status") != "ok":
        return "fix_local_files"
    status = rec.get("transistor_status")
    policy = rec.get("podcast_policy")
    if status == "published":
        return "none"
    if policy == "ready_public_normal":
        return "publish_to_transistor"
    return "none"


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_local_records": len(records),
        "valid_local_records": sum(1 for r in records if r.get("local_status") == "ok"),
        "by_folder_category": dict(Counter(r.get("folder_category") for r in records)),
        "by_youtube_privacy": dict(Counter(r.get("youtube_privacy") for r in records)),
        "by_transcript_status": dict(Counter(r.get("transcript_status") for r in records)),
        "by_content_class": dict(Counter(r.get("content_class") for r in records)),
        "by_podcast_policy": dict(Counter(r.get("podcast_policy") for r in records)),
        "by_transistor_status": dict(Counter(r.get("transistor_status") for r in records)),
        "by_action_needed": dict(Counter(r.get("action_needed") for r in records)),
    }


def write_markdown(path: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    actions = [r for r in records if r.get("action_needed") != "none"]
    lines = [
        "# Library Audit",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Summary",
        "",
    ]
    for key in [
        "total_local_records",
        "valid_local_records",
        "by_folder_category",
        "by_youtube_privacy",
        "by_transcript_status",
        "by_content_class",
        "by_podcast_policy",
        "by_transistor_status",
        "by_action_needed",
    ]:
        lines.append(f"- `{key}`: `{json.dumps(summary[key], ensure_ascii=False)}`")

    lines += [
        "",
        "## Action Needed",
        "",
        "| action | date | privacy | content | transcript | transistor | video_id | title | folder |",
        "|---|---:|---|---|---|---|---|---|---|",
    ]
    for rec in sorted(actions, key=lambda r: (r.get("action_needed", ""), r.get("upload_date", "")), reverse=True):
        lines.append(
            "| {action} | {date} | {privacy} | {content} | {transcript} | {transistor} | `{vid}` | {title} | `{folder}` |".format(
                action=rec.get("action_needed", ""),
                date=rec.get("upload_date", ""),
                privacy=rec.get("youtube_privacy", ""),
                content=rec.get("content_class", ""),
                transcript=rec.get("transcript_status", ""),
                transistor=rec.get("transistor_status", ""),
                vid=rec.get("video_id", ""),
                title=(rec.get("title", "") or "").replace("|", "\\|")[:80],
                folder=rec.get("folder", ""),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build canonical media-library manifest")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--skip-transistor", action="store_true")
    args = parser.parse_args()

    load_env()
    records = local_records()
    youtube = load_youtube_snapshot()
    transistor = {} if args.skip_transistor else fetch_transistor_map()
    records = add_remote_and_actions(records, youtube, transistor)
    summary = summarize(records)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out_dir / "library_manifest.json"
    audit_path = args.out_dir / "library_audit.md"
    manifest_path.write_text(
        json.dumps({"summary": summary, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(audit_path, summary, records)

    print(f"Wrote {manifest_path}")
    print(f"Wrote {audit_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
