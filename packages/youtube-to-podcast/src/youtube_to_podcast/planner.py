"""Read-only reconciliation planner with conservative history semantics."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .hashing import seal_plan, sha256_text
from .transistor import TransistorClient
from .youtube import ListedVideo


MetadataFetcher = Callable[[str], dict[str, Any]]
MediaPreparer = Callable[[str, Path], dict[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _published_at(upload_date: str) -> str | None:
    try:
        parsed = datetime.strptime(upload_date, "%Y%m%d")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT00:00:00Z")


def _compact_episode(episode: dict[str, Any]) -> dict[str, Any]:
    attrs = episode.get("attributes") or {}
    return {
        "episode_id": str(episode.get("id") or ""),
        "status": attrs.get("status"),
        "title": attrs.get("title") or "",
        "media_url_present": bool(attrs.get("media_url")),
        "updated_at": attrs.get("updated_at"),
    }


def _relative(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    return str(path.resolve().relative_to(root.resolve()))


def _candidate_safety_reasons(
    metadata: dict[str, Any],
    config: Config,
) -> list[str]:
    reasons: list[str] = []
    if not metadata.get("title"):
        reasons.append("missing_title")
    if not _published_at(str(metadata.get("upload_date") or "")):
        reasons.append("missing_publish_date")
    availability = metadata.get("availability")
    if availability not in (None, "public"):
        reasons.append(f"youtube_not_public:{availability}")
    is_live = (
        metadata.get("is_live")
        or metadata.get("was_live")
        or metadata.get("live_status") not in (None, "not_live")
    )
    if is_live and not config.policy.include_live:
        reasons.append("live_content_disabled")
    return reasons


def build_plan(
    config: Config,
    client: TransistorClient,
    listed_videos: list[ListedVideo],
    *,
    fetch_metadata: MetadataFetcher,
    prepare_media: MediaPreparer,
) -> dict[str, Any]:
    episodes, remote_by_video = client.episodes_by_video_id(config.show_id)
    listing_by_id = {item.video_id: item for item in listed_videos}
    published_positions: list[int] = []
    for video_id, remote_items in remote_by_video.items():
        listed = listing_by_id.get(video_id)
        if not listed or len(remote_items) != 1:
            continue
        status = (remote_items[0].get("attributes") or {}).get("status")
        if status == "published":
            published_positions.append(listed.playlist_index)
    newest_published_index = min(published_positions) if published_positions else None

    blocked: list[dict[str, Any]] = []
    raw_candidates: list[dict[str, Any]] = []
    historical_gap_count = 0
    synced_count = 0
    draft_count = 0

    for listed in listed_videos:
        remote_items = remote_by_video.get(listed.video_id, [])
        if len(remote_items) > 1:
            blocked.append(
                {
                    "video_id": listed.video_id,
                    "title": listed.title,
                    "playlist_index": listed.playlist_index,
                    "reasons": ["multiple_remote_episodes"],
                }
            )
            continue
        if len(remote_items) == 1:
            compact = _compact_episode(remote_items[0])
            if compact["status"] == "published":
                synced_count += 1
                continue
            if compact["status"] == "draft":
                draft_count += 1
                blocked.append(
                    {
                        "video_id": listed.video_id,
                        "title": listed.title,
                        "playlist_index": listed.playlist_index,
                        "reasons": ["existing_draft_requires_manual_review"],
                    }
                )
                continue
            blocked.append(
                {
                    "video_id": listed.video_id,
                    "title": listed.title,
                    "playlist_index": listed.playlist_index,
                    "reasons": [f"unsupported_remote_status:{compact['status']}"],
                }
            )
            continue

        if config.policy.mode == "backfill":
            raw_candidates.append({"listed": listed, "remote_precondition": None})
            continue
        if newest_published_index is None:
            historical_gap_count += 1
            if len(blocked) < 100:
                blocked.append(
                    {
                        "video_id": listed.video_id,
                        "title": listed.title,
                        "playlist_index": listed.playlist_index,
                        "reasons": [
                            "empty_or_unlinked_show_requires_backfill_mode"
                        ],
                    }
                )
            continue
        if listed.playlist_index < newest_published_index:
            raw_candidates.append({"listed": listed, "remote_precondition": None})
        else:
            historical_gap_count += 1
            if len(blocked) < 100:
                blocked.append(
                    {
                        "video_id": listed.video_id,
                        "title": listed.title,
                        "playlist_index": listed.playlist_index,
                        "reasons": [
                            "historical_gap_requires_backfill_mode"
                        ],
                    }
                )

    # Oldest eligible item first preserves episode-number and publish-date order.
    ordered_candidates = sorted(
        raw_candidates,
        key=lambda item: item["listed"].playlist_index,
        reverse=True,
    )
    selected = ordered_candidates[: config.policy.max_candidates]
    actions: list[dict[str, Any]] = []
    examined_candidate_count = 0

    for candidate in selected:
        if len(actions) >= config.policy.max_actions:
            break
        examined_candidate_count += 1
        listed = candidate["listed"]
        try:
            metadata = fetch_metadata(listed.video_id)
        except Exception as exc:
            blocked.append(
                {
                    "video_id": listed.video_id,
                    "title": listed.title,
                    "playlist_index": listed.playlist_index,
                    "reasons": [
                        f"youtube_metadata_failed:{type(exc).__name__}"
                    ],
                }
            )
            continue
        reasons = _candidate_safety_reasons(metadata, config)
        if reasons:
            blocked.append(
                {
                    "video_id": listed.video_id,
                    "title": metadata.get("title") or listed.title,
                    "playlist_index": listed.playlist_index,
                    "reasons": reasons,
                }
            )
            continue
        try:
            media = prepare_media(
                listed.video_id,
                config.work_dir / "media" / listed.video_id,
            )
        except Exception as exc:
            blocked.append(
                {
                    "video_id": listed.video_id,
                    "title": metadata.get("title") or listed.title,
                    "playlist_index": listed.playlist_index,
                    "reasons": [
                        f"media_preparation_failed:{type(exc).__name__}"
                    ],
                }
            )
            continue

        description = str(metadata.get("description") or "")
        remote_precondition = candidate["remote_precondition"]
        if config.policy.publication == "publish":
            action = "create_and_publish"
        else:
            action = "create_draft"
        actions.append(
            {
                "action": action,
                "video_id": listed.video_id,
                "playlist_index": listed.playlist_index,
                "title": metadata["title"],
                "description": description,
                "description_sha256": sha256_text(description),
                "video_url": metadata["video_url"],
                "image_url": metadata["image_url"],
                "upload_date": metadata["upload_date"],
                "published_at": _published_at(metadata["upload_date"]),
                "audio_path": _relative(media["audio_path"], config.root),
                "audio_sha256": media["audio_sha256"],
                "audio_bytes": media["audio_bytes"],
                "transcript_path": _relative(
                    media.get("transcript_path"),
                    config.root,
                ),
                "transcript_sha256": media.get("transcript_sha256"),
                "transcript_chars": media.get("transcript_chars", 0),
                "warnings": [
                    warning
                    for warning, present in (
                        ("missing_description", not description),
                        (
                            "missing_transcript",
                            not media.get("transcript_path"),
                        ),
                    )
                    if present
                ],
                "remote_precondition": remote_precondition,
            }
        )

    deferred_candidate_count = max(
        0,
        len(ordered_candidates) - examined_candidate_count,
    )
    plan = {
        "schema_version": 1,
        "kind": "youtube_to_podcast_plan",
        "generated_at": utc_now(),
        "show_id": config.show_id,
        "channel_url": config.channel_url,
        "mode": config.policy.mode,
        "publication": config.policy.publication,
        "actions": actions,
        "blocked": blocked,
        "summary": {
            "youtube_video_count": len(listed_videos),
            "remote_episode_count": len(episodes),
            "remote_linked_video_count": len(remote_by_video),
            "synced_published_count": synced_count,
            "existing_draft_count": draft_count,
            "action_count": len(actions),
            "blocked_count": len(blocked),
            "historical_gap_count": historical_gap_count,
            "deferred_candidate_count": deferred_candidate_count,
            "examined_candidate_count": examined_candidate_count,
        },
    }
    return seal_plan(plan)


def write_plan(plan: dict[str, Any], work_dir: Path) -> tuple[Path, Path]:
    plan_dir = work_dir / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = plan_dir / f"plan-{stamp}.json"
    markdown_path = plan_dir / f"plan-{stamp}.md"
    payload = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=plan_dir,
        prefix=".plan.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    temp_path.replace(json_path)
    latest = plan_dir / "latest.json"
    latest.write_text(payload, encoding="utf-8")

    summary = plan["summary"]
    lines = [
        "# YouTube to Podcast plan",
        "",
        f"- Plan hash: `{plan['plan_hash']}`",
        f"- Approval hash: `{plan['approval_hash']}`",
        f"- Mode: `{plan['mode']}`",
        f"- Publication: `{plan['publication']}`",
        f"- Actions: **{summary['action_count']}**",
        f"- Blocked: **{summary['blocked_count']}**",
        f"- Historical gaps quarantined: **{summary['historical_gap_count']}**",
        "",
        "## Actions",
        "",
    ]
    if plan["actions"]:
        for item in plan["actions"]:
            lines.append(
                f"- `{item['action']}` · `{item['video_id']}` · {item['title']}"
            )
    else:
        lines.append("- None")
    lines += ["", "## Blocked", ""]
    if plan["blocked"]:
        for item in plan["blocked"][:100]:
            lines.append(
                f"- `{item['video_id']}` · {item['title']} · "
                f"{', '.join(item['reasons'])}"
            )
    else:
        lines.append("- None")
    markdown = "\n".join(lines) + "\n"
    markdown_path.write_text(markdown, encoding="utf-8")
    (plan_dir / "latest.md").write_text(markdown, encoding="utf-8")
    return json_path, markdown_path
