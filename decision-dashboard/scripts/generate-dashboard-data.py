#!/usr/bin/env python3
"""Export a local, read-only decision snapshot for the podcast dashboard."""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.check.build_library_manifest import local_records  # noqa: E402
from tools.podcast.core import (  # noqa: E402
    atomic_write_json,
    extract_video_id,
    load_env,
    require_transistor_config,
)
from tools.podcast.transistor_client import TransistorClient  # noqa: E402


PLAN_DIR = PROJECT_ROOT / "logs" / "podcast_sync" / "plans"
LEDGER_DIR = PROJECT_ROOT / "logs" / "podcast_sync" / "ledgers"
YOUTUBE_SNAPSHOT = PROJECT_ROOT / "tools" / "youtube" / "all_videos_full.json"
LAUNCH_AGENT = (
    Path.home()
    / "Library"
    / "LaunchAgents"
    / "com.sunyuzheng.kedaibiao-podcast-sync.plist"
)
OUTPUT = DASHBOARD_ROOT / "app" / "podcast-state.local.json"


def latest_matching(directory: Path, pattern: str) -> Path:
    paths = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime)
    if not paths:
        raise FileNotFoundError(f"No {pattern} files in {directory}")
    return paths[-1]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def description_source(
    video_id: str,
    local_by_id: dict[str, dict[str, Any]],
    youtube_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    local = local_by_id.get(video_id) or {}
    folder_value = local.get("folder")
    if folder_value:
        folder = PROJECT_ROOT / folder_value
        for path in sorted(folder.glob("*.description")):
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return {
                    "kind": "local_description",
                    "chars": len(text),
                    "preview": " ".join(text.split())[:220],
                    "path": str(path.relative_to(PROJECT_ROOT)),
                }
    text = str((youtube_by_id.get(video_id) or {}).get("description") or "").strip()
    if text:
        return {
            "kind": "youtube_snapshot",
            "chars": len(text),
            "preview": " ".join(text.split())[:220],
            "path": "tools/youtube/all_videos_full.json",
        }
    return {
        "kind": "missing",
        "chars": 0,
        "preview": "",
        "path": None,
    }


def transcript_outcome() -> dict[str, Any]:
    ledger_path = latest_matching(LEDGER_DIR, "transcript-*.jsonl")
    events = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    updated = [event for event in events if event.get("event") == "transcript_updated"]
    completed = next(
        (event for event in reversed(events) if event.get("event") == "execution_completed"),
        {},
    )
    return {
        "updated": len(updated),
        "skipped": int(completed.get("skipped") or 0),
        "failed": 0,
        "uniqueEpisodeIds": len({event.get("episode_id") for event in updated}),
        "uniqueVideoIds": len({event.get("video_id") for event in updated}),
        "completedAt": completed.get("timestamp"),
        "ledgerPath": str(ledger_path.relative_to(PROJECT_ROOT)),
    }


def publish_outcome() -> dict[str, Any]:
    ledger_path = latest_matching(LEDGER_DIR, "publish-*.jsonl")
    events = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    completed = next(
        (event for event in reversed(events) if event.get("event") == "execution_completed"),
        {},
    )
    return {
        "completed": bool(completed),
        "published": sum(event.get("event") == "episode_published" for event in events),
        "createdDrafts": sum(event.get("event") == "draft_created" for event in events),
        "repairedDrafts": sum(event.get("event") == "draft_repaired" for event in events),
        "reordered": sum(event.get("event") == "episode_reordered" for event in events),
        "completedAt": completed.get("timestamp"),
        "planHash": completed.get("plan_hash"),
        "ledgerPath": str(ledger_path.relative_to(PROJECT_ROOT)),
    }


def main() -> int:
    load_env()
    api_key, configured_show_id = require_transistor_config()

    plan_path = latest_matching(PLAN_DIR, "sync-plan-*.json")
    plan = read_json(plan_path)
    if str(plan.get("show_id")) != str(configured_show_id):
        raise RuntimeError(
            f"Plan show {plan.get('show_id')} does not match configured show {configured_show_id}"
        )

    remote = TransistorClient(api_key).list_episodes(str(configured_show_id))
    remote_by_video: dict[str, list[dict[str, Any]]] = {}
    for episode in remote:
        video_id = extract_video_id(episode.get("attributes", {}).get("video_url"))
        if video_id:
            remote_by_video.setdefault(video_id, []).append(episode)

    youtube_records = read_json(YOUTUBE_SNAPSHOT)
    youtube_by_id = {
        str(record.get("video_id")): record
        for record in youtube_records
        if record.get("video_id")
    }
    local_by_id = {
        str(record.get("video_id")): record
        for record in local_records()
        if record.get("video_id")
    }

    projected_by_video = {
        item.get("video_id"): item for item in plan.get("projected_feed", [])
    }
    candidate_by_video = {
        item.get("local", {}).get("video_id"): item
        for item in plan.get("candidate_publish_actions", [])
    }

    publish_items = []
    for item in plan.get("publish_actions", []):
        local = item.get("local", {})
        video_id = str(local.get("video_id") or "")
        projected = projected_by_video.get(video_id) or {}
        publish_items.append(
            {
                "videoId": video_id,
                "title": local.get("base_title") or "(无标题)",
                "action": item.get("action"),
                "episodeId": (item.get("remote_precondition") or {}).get("episode_id"),
                "publishedAt": local.get("published_at"),
                "targetNumber": projected.get("target_number"),
                "descriptionChars": int(local.get("description_chars") or 0),
                "transcriptChars": int(local.get("transcript_chars") or 0),
                "transcriptSource": local.get("transcript_source_status") or "missing",
                "audioBytes": int(local.get("audio_bytes") or 0),
                "warnings": item.get("warnings") or [],
                "youtubeStatus": (item.get("youtube_verification") or {}).get("status"),
                "verifiedAt": (item.get("youtube_verification") or {}).get("verified_at"),
                "videoUrl": local.get("video_url"),
                "imageUrl": local.get("image_url"),
            }
        )

    blocked_items = []
    for item in plan.get("blocked", []):
        video_id = str(item.get("video_id") or "")
        candidate = candidate_by_video.get(video_id) or {}
        local = candidate.get("local") or {}
        matching_remote = remote_by_video.get(video_id) or []
        drafts = [
            episode
            for episode in matching_remote
            if episode.get("attributes", {}).get("status") == "draft"
        ]
        blocked_items.append(
            {
                "videoId": video_id,
                "title": item.get("title") or local.get("base_title") or "(无标题)",
                "reasons": item.get("reasons") or [],
                "remoteDraftCount": len(drafts),
                "remoteDraftIds": [str(episode.get("id")) for episode in drafts],
                "publishedAt": local.get("published_at"),
                "descriptionChars": int(local.get("description_chars") or 0),
                "transcriptChars": int(local.get("transcript_chars") or 0),
                "transcriptSource": local.get("transcript_source_status") or "missing",
                "warnings": candidate.get("warnings") or [],
                "videoUrl": local.get("video_url")
                or f"https://www.youtube.com/watch?v={video_id}",
            }
        )

    metadata_gaps = []
    for episode in remote:
        attrs = episode.get("attributes", {})
        if attrs.get("status") != "published" or str(attrs.get("description") or "").strip():
            continue
        video_id = extract_video_id(attrs.get("video_url")) or ""
        source = description_source(video_id, local_by_id, youtube_by_id)
        metadata_gaps.append(
            {
                "episodeId": str(episode.get("id") or ""),
                "videoId": video_id,
                "number": attrs.get("number"),
                "title": attrs.get("title") or "(无标题)",
                "publishedAt": attrs.get("published_at"),
                "videoUrl": attrs.get("video_url"),
                "source": source,
                "recommendedAction": (
                    "backfill_from_source" if source["chars"] else "write_manually"
                ),
            }
        )

    privacy_counts = Counter(
        str(record.get("privacy") or "unknown") for record in youtube_records
    )
    status_counts = Counter(
        str(episode.get("attributes", {}).get("status") or "unknown")
        for episode in remote
    )
    transcript_exceptions = []
    for episode in remote:
        attrs = episode.get("attributes", {})
        if attrs.get("status") != "published":
            continue
        has_artifact = bool(
            attrs.get("transcript_url")
            or attrs.get("transcripts")
            or attrs.get("transcript_text") is not None
        )
        if not has_artifact:
            transcript_exceptions.append(
                {
                    "episodeId": str(episode.get("id") or ""),
                    "videoId": extract_video_id(attrs.get("video_url")) or "",
                    "title": attrs.get("title") or "(无标题)",
                    "reason": "no_remote_transcript_artifact",
                }
            )
    transcript_artifact_count = (
        int(status_counts.get("published", 0)) - len(transcript_exceptions)
    )

    yt_dlp = subprocess.run(
        [str(PROJECT_ROOT / ".venv-podcast" / "bin" / "yt-dlp"), "--version"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    schedule = {"weekday": 0, "hour": 9, "minute": 15}
    if LAUNCH_AGENT.exists():
        plist = plistlib.loads(LAUNCH_AGENT.read_bytes())
        interval = plist.get("StartCalendarInterval") or {}
        schedule = {
            "weekday": int(interval.get("Weekday", 0)),
            "hour": int(interval.get("Hour", 9)),
            "minute": int(interval.get("Minute", 15)),
        }

    publish_warning_counts = Counter(
        warning
        for item in publish_items
        for warning in item.get("warnings", [])
    )
    blocked_reason_counts = Counter(
        reason
        for item in blocked_items
        for reason in item.get("reasons", [])
    )
    recoverable_metadata = sum(
        item["source"]["chars"] > 0 for item in metadata_gaps
    )

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "showId": str(configured_show_id),
        "plan": {
            "generatedAt": plan.get("generated_at"),
            "planPath": str(plan_path.relative_to(PROJECT_ROOT)),
            "planHash": plan.get("plan_hash"),
            "publishApprovalHash": plan.get("publish_approval_hash"),
            "transcriptApprovalHash": plan.get("transcript_approval_hash"),
            "youtubeSnapshot": plan.get("youtube_snapshot"),
            "projectedPublishedCount": plan.get("projected_published_episode_count"),
            "projectedReorderCount": len(plan.get("projected_reorder_actions", [])),
        },
        "summary": {
            "remotePublished": int(status_counts.get("published", 0)),
            "remoteDrafts": int(status_counts.get("draft", 0)),
            "transcriptArtifacts": transcript_artifact_count,
            "publishReady": len(publish_items),
            "publishBlocked": len(blocked_items),
            "metadataGaps": len(metadata_gaps),
            "metadataRecoverable": recoverable_metadata,
            "metadataManual": len(metadata_gaps) - recoverable_metadata,
            "publishWarningCounts": dict(publish_warning_counts),
            "blockedReasonCounts": dict(blocked_reason_counts),
        },
        "transcriptOutcome": transcript_outcome(),
        "transcriptExceptions": transcript_exceptions,
        "publishOutcome": publish_outcome(),
        "publishItems": publish_items,
        "blockedItems": blocked_items,
        "metadataGaps": metadata_gaps,
        "system": {
            "ytDlpVersion": yt_dlp.stdout.strip() if yt_dlp.returncode == 0 else "unavailable",
            "schedule": schedule,
            "scheduleLabel": "每周日 09:15（本地时间）",
            "schedulerMode": "plan_only",
            "oauthScope": "youtube.readonly",
            "youtubeTotal": len(youtube_records),
            "youtubePrivacy": dict(privacy_counts),
            "candidateEvidence": (
                plan.get("youtube_snapshot", {}).get("candidate_verification") or {}
            ),
            "publicListing": (
                plan.get("youtube_snapshot", {}).get("public_listing_snapshot") or {}
            ),
            "oauthSnapshot": (
                plan.get("youtube_snapshot", {}).get("oauth_snapshot") or {}
            ),
        },
    }
    atomic_write_json(OUTPUT, payload)
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "publishReady": len(publish_items),
                "publishBlocked": len(blocked_items),
                "metadataGaps": len(metadata_gaps),
                "metadataRecoverable": recoverable_metadata,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
