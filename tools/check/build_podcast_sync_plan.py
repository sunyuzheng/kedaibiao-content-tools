#!/usr/bin/env python3
"""Build one immutable, approval-gated YouTube -> Transistor sync plan.

This command is read-only with respect to YouTube and Transistor. It fetches the
Transistor collection once, combines it with the canonical local manifest
semantics, and writes deterministic execution inputs under logs/.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.check.build_library_manifest import (  # noqa: E402
    add_remote_and_actions,
    load_youtube_snapshot,
    local_records,
    summarize,
    write_markdown as write_manifest_markdown,
)
from tools.podcast.core import (  # noqa: E402
    YOUTUBE_SNAPSHOT,
    atomic_write_json,
    canonical_json,
    choose_transcript_path,
    first_existing,
    load_env,
    numbered_title,
    plan_hash,
    published_at_from_yyyymmdd,
    relative_to_project,
    require_transistor_config,
    sha256_file,
    sha256_text,
    strip_episode_number,
    timed_text_to_text,
    utc_now,
)
from tools.podcast.transistor_client import TransistorClient  # noqa: E402
from tools.podcast.show_notes import validate_show_notes  # noqa: E402


DEFAULT_OUT_DIR = PROJECT_ROOT / "logs" / "podcast_sync" / "plans"
PUBLIC_YOUTUBE_SNAPSHOT = (
    PROJECT_ROOT / "tools" / "youtube" / "public_videos_snapshot.json"
)
CANDIDATE_VERIFICATION_SNAPSHOT = (
    PROJECT_ROOT / "tools" / "youtube" / "podcast_candidate_verification.json"
)


def compact_episode(episode: dict[str, Any]) -> dict[str, Any]:
    attrs = episode.get("attributes", {})
    description = str(attrs.get("description") or "").strip()
    transcript = attrs.get("transcript_text")
    transcript_url = attrs.get("transcript_url")
    transcripts = attrs.get("transcripts") or []
    return {
        "episode_id": str(episode.get("id") or ""),
        "status": attrs.get("status"),
        "number": attrs.get("number"),
        "title": attrs.get("title"),
        "video_url": attrs.get("video_url"),
        "published_at": attrs.get("published_at"),
        "updated_at": attrs.get("updated_at"),
        "duration_seconds": attrs.get("duration"),
        "media_url_present": bool(attrs.get("media_url")),
        "description_chars": len(description),
        "description_sha256": sha256_text(description),
        "transcript_observed": transcript is not None,
        "transcript_present": bool(transcript or transcript_url or transcripts),
        "transcript_url": transcript_url,
        "transcript_formats_count": len(transcripts),
        "transcript_chars": len(transcript or ""),
        "transcript_sha256": sha256_text(transcript) if transcript else None,
    }


def manifest_remote_map(
    episodes_by_video: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        video_id: [
            {
                "episode_id": item["episode_id"],
                "status": item["status"],
                "number": item["number"],
                "title": item["title"],
                "published_at": item["published_at"],
            }
            for item in (compact_episode(episode) for episode in episodes)
        ]
        for video_id, episodes in episodes_by_video.items()
    }


def file_snapshot_info(path: Path, max_age_hours: float) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": relative_to_project(path),
            "exists": False,
            "age_hours": None,
            "max_age_hours": max_age_hours,
            "fresh": False,
        }
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
    return {
        "path": relative_to_project(path),
        "exists": True,
        "modified_at": mtime.isoformat(timespec="seconds"),
        "age_hours": round(age_hours, 2),
        "max_age_hours": max_age_hours,
        "fresh": age_hours <= max_age_hours,
    }


def current_youtube_state(
    max_age_hours: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    oauth = file_snapshot_info(YOUTUBE_SNAPSHOT, max_age_hours)
    public_listing = file_snapshot_info(PUBLIC_YOUTUBE_SNAPSHOT, max_age_hours)
    oauth_records = load_youtube_snapshot()
    if public_listing["fresh"]:
        payload = json.loads(PUBLIC_YOUTUBE_SNAPSHOT.read_text(encoding="utf-8"))
        if payload.get("channel_id") != "UC_5lJHgnMP_lb_VpIiXV0hQ":
            raise RuntimeError("Public YouTube listing channel id mismatch")
        public_records: dict[str, dict[str, Any]] = {}
        for item in payload.get("videos", []):
            video_id = item.get("video_id")
            if not video_id:
                continue
            previous = oauth_records.get(video_id, {})
            public_records[video_id] = {
                "video_id": video_id,
                "title": item.get("title") or previous.get("title"),
                "published_at": previous.get("published_at"),
                "description": previous.get("description"),
                "privacy": "public",
            }
        selected = public_records
        source = "anonymous_current_listing"
    elif oauth["fresh"]:
        selected = oauth_records
        source = "oauth_full_snapshot"
    else:
        selected = oauth_records
        source = "stale_oauth_snapshot"
    return selected, {
        "source": source,
        "fresh": source != "stale_oauth_snapshot",
        "age_hours": (
            public_listing["age_hours"]
            if source == "anonymous_current_listing"
            else oauth["age_hours"]
        ),
        "max_age_hours": max_age_hours,
        "oauth_snapshot": oauth,
        "public_listing_snapshot": public_listing,
    }


def candidate_verification_state(
    max_age_hours: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    info = file_snapshot_info(CANDIDATE_VERIFICATION_SNAPSHOT, max_age_hours)
    if not info["exists"]:
        return {}, info
    payload = json.loads(CANDIDATE_VERIFICATION_SNAPSHOT.read_text(encoding="utf-8"))
    if payload.get("channel_id") != "UC_5lJHgnMP_lb_VpIiXV0hQ":
        raise RuntimeError("Candidate verification channel id mismatch")
    rows = {
        item["video_id"]: item
        for item in payload.get("candidates", [])
        if item.get("video_id")
    }
    info.update({
        "source": payload.get("source"),
        "candidate_count": payload.get("candidate_count", len(rows)),
        "public_count": payload.get("public_count", 0),
        "member_only_count": payload.get("member_only_count", 0),
        "unverified_count": payload.get("unverified_count", 0),
        "source_plan_hash": payload.get("source_plan_hash"),
    })
    return rows, info


def local_payload(
    record: dict[str, Any],
    youtube_record: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    folder = PROJECT_ROOT / record["folder"]
    video_id = record["video_id"]
    audio = first_existing(
        folder,
        ("*.m4a", "*.mp3", "*.aac", "*.opus", "*.wav", "*.mp4"),
    )
    versioned_podcast_description = (
        PROJECT_ROOT / "podcast_show_notes" / f"{video_id}.txt"
    )
    podcast_description_path = (
        versioned_podcast_description
        if versioned_podcast_description.is_file()
        and versioned_podcast_description.stat().st_size > 0
        else first_existing(folder, ("*.podcast-description.txt",))
    )
    youtube_description_path = first_existing(folder, ("*.description",))
    transcript_path = choose_transcript_path(folder, record)
    podcast_description = (
        podcast_description_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()
        if podcast_description_path
        else ""
    )
    local_youtube_description = (
        youtube_description_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()
        if youtube_description_path
        else ""
    )
    youtube_description = str(
        (youtube_record or {}).get("description") or ""
    ).strip()
    if podcast_description:
        description = podcast_description
        description_source = "podcast_sidecar"
        description_text = None
        description_path = podcast_description_path
    elif local_youtube_description:
        description = local_youtube_description
        description_source = "local_youtube_file"
        description_text = None
        description_path = youtube_description_path
    elif youtube_description:
        description = youtube_description
        description_source = "youtube_snapshot"
        description_text = youtube_description
        description_path = None
    else:
        description = ""
        description_source = "empty"
        description_text = None
        description_path = None
    transcript = timed_text_to_text(transcript_path) if transcript_path else ""
    base_title = strip_episode_number(record.get("title") or folder.name)
    warnings: list[str] = []
    if not audio:
        warnings.append("missing_audio")
    if not description:
        warnings.append("missing_description")
    description_quality = (
        validate_show_notes(description)
        if description_source == "podcast_sidecar"
        else None
    )
    if description_quality:
        warnings.extend(
            f"podcast_description_error:{item}"
            for item in description_quality["errors"]
        )
        warnings.extend(
            f"podcast_description_warning:{item}"
            for item in description_quality["warnings"]
        )
    if not transcript:
        warnings.append("missing_transcript")
    published_at = published_at_from_yyyymmdd(record.get("upload_date") or "")
    if not published_at:
        warnings.append("missing_publish_date")

    payload = {
        "video_id": video_id,
        "folder": record["folder"],
        "audio_path": relative_to_project(audio),
        "audio_sha256": sha256_file(audio) if audio else None,
        "audio_bytes": audio.stat().st_size if audio else 0,
        "description_path": relative_to_project(description_path),
        "description_source": description_source,
        "description_text": description_text,
        "description_sha256": sha256_text(description),
        "description_chars": len(description),
        "description_quality": description_quality,
        "transcript_path": relative_to_project(transcript_path),
        "transcript_source_status": record.get("transcript_status"),
        "transcript_sha256": sha256_text(transcript) if transcript else None,
        "transcript_chars": len(transcript),
        "base_title": base_title,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "image_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "published_at": published_at,
    }
    return payload, warnings


def build_plan(
    max_snapshot_age_hours: float,
    *,
    manifest_out_dir: Path | None = None,
) -> dict[str, Any]:
    load_env()
    api_key, show_id = require_transistor_config()
    client = TransistorClient(api_key)
    episodes, episodes_by_video = client.episodes_by_video_id(show_id)
    remote_map = manifest_remote_map(episodes_by_video)

    youtube_state, youtube_snapshot = current_youtube_state(max_snapshot_age_hours)
    candidate_verifications, candidate_verification_info = (
        candidate_verification_state(max_snapshot_age_hours)
    )
    youtube_snapshot["candidate_verification"] = candidate_verification_info
    records = add_remote_and_actions(
        local_records(),
        youtube_state,
        remote_map,
    )
    if manifest_out_dir is not None:
        manifest_out_dir.mkdir(parents=True, exist_ok=True)
        manifest_summary = summarize(records)
        atomic_write_json(
            manifest_out_dir / "library_manifest.json",
            {"summary": manifest_summary, "records": records},
        )
        write_manifest_markdown(
            manifest_out_dir / "library_audit.md",
            manifest_summary,
            records,
        )
    publish_actions: list[dict[str, Any]] = []
    candidate_publish_actions: list[dict[str, Any]] = []
    description_actions: list[dict[str, Any]] = []
    transcript_actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for record in sorted(records, key=lambda item: (item.get("upload_date") or "", item.get("video_id") or "")):
        video_id = record.get("video_id")
        if not video_id:
            continue
        remote = [compact_episode(ep) for ep in episodes_by_video.get(video_id, [])]
        published = [ep for ep in remote if ep.get("status") == "published"]
        drafts = [ep for ep in remote if ep.get("status") == "draft"]
        payload, local_warnings = local_payload(
            record,
            youtube_state.get(video_id),
        )

        if record.get("action_needed") == "publish_to_transistor":
            # Description/transcript quality is reported but is not a publish
            # policy gate. Only safety/idempotency preconditions block.
            reasons: list[str] = []
            if not youtube_snapshot["fresh"]:
                reasons.append("stale_youtube_snapshot")
            if len(drafts) > 1:
                reasons.append("multiple_remote_drafts")
            if not payload["audio_path"] and not (
                len(drafts) == 1 and drafts[0].get("media_url_present")
            ):
                reasons.append("no_audio_for_create_or_repair")
            if not payload["published_at"]:
                reasons.append("missing_publish_date")

            item = {
                "action": "update_draft_then_publish" if len(drafts) == 1 else "create_draft_then_publish",
                "local": payload,
                "remote_precondition": drafts[0] if len(drafts) == 1 else None,
                "warnings": sorted(set(local_warnings)),
                "youtube_verification": candidate_verifications.get(video_id),
            }
            candidate_publish_actions.append(item)
            verification = candidate_verifications.get(video_id) or {}
            if (
                not candidate_verification_info.get("fresh")
                or verification.get("status") != "public"
            ):
                reasons.append("youtube_candidate_not_verified")
            if reasons:
                blocked.append({
                    "scope": "publish",
                    "video_id": video_id,
                    "title": payload["base_title"],
                    "reasons": sorted(set(reasons)),
                })
            else:
                publish_actions.append(item)

        # A podcast-specific sidecar is an explicit request to update the
        # canonical published episode in place. Ordinary YouTube descriptions
        # never trigger historical rewrites.
        if payload["description_source"] == "podcast_sidecar" and published:
            quality_errors = (payload.get("description_quality") or {}).get(
                "errors",
                [],
            )
            last_timestamp = (payload.get("description_quality") or {}).get(
                "last_timestamp_seconds"
            )
            remote_duration = published[0].get("duration_seconds") if len(published) == 1 else None
            if (
                last_timestamp is not None
                and remote_duration is not None
                and last_timestamp > int(remote_duration)
            ):
                quality_errors = [
                    *quality_errors,
                    "timestamp_after_episode_duration",
                ]
            if quality_errors:
                blocked.append({
                    "scope": "description",
                    "video_id": video_id,
                    "title": payload["base_title"],
                    "reasons": [
                        f"invalid_podcast_description:{item}"
                        for item in quality_errors
                    ],
                })
            elif len(published) != 1:
                blocked.append({
                    "scope": "description",
                    "video_id": video_id,
                    "title": payload["base_title"],
                    "reasons": ["multiple_remote_published_episodes"],
                })
            else:
                target = published[0]
                if target["description_sha256"] != payload["description_sha256"]:
                    description_actions.append({
                        "action": "update_description",
                        "video_id": video_id,
                        "episode_id": target["episode_id"],
                        "episode_status": target["status"],
                        "episode_title": target["title"],
                        "description_path": payload["description_path"],
                        "description_source": payload["description_source"],
                        "description_sha256": payload["description_sha256"],
                        "description_chars": payload["description_chars"],
                        "remote_description_sha256": target["description_sha256"],
                        "remote_description_chars": target["description_chars"],
                        "remote_updated_at": target["updated_at"],
                    })

        # Existing published episode is the canonical target for transcript backfill.
        # Draft-only episodes get their transcript through the publish action above.
        if payload["transcript_path"] and published:
            if len(published) != 1:
                blocked.append({
                    "scope": "transcript",
                    "video_id": video_id,
                    "title": payload["base_title"],
                    "reasons": ["multiple_remote_published_episodes"],
                })
                continue
            target = published[0]
            if (
                target.get("transcript_observed")
                and target.get("transcript_sha256") == payload["transcript_sha256"]
            ):
                continue
            if target.get("transcript_present") and not target.get("transcript_observed"):
                # The public API exposes URLs/formats, not the original source text.
                # Preserve an existing transcript rather than overwrite something
                # whose exact content cannot be compared.
                continue
            transcript_actions.append({
                "action": "update_transcript",
                "video_id": video_id,
                "episode_id": target["episode_id"],
                "episode_status": target["status"],
                "episode_title": target["title"],
                "transcript_path": payload["transcript_path"],
                "transcript_source_status": payload["transcript_source_status"],
                "transcript_sha256": payload["transcript_sha256"],
                "transcript_chars": payload["transcript_chars"],
                "remote_transcript_observed": target["transcript_observed"],
                "remote_transcript_present": target["transcript_present"],
                "remote_transcript_sha256": target["transcript_sha256"],
                "remote_updated_at": target["updated_at"],
            })

    # Project the final published ordering so publication and the resulting
    # public-feed renumber are reviewed as one material change.
    record_by_video = {
        record["video_id"]: record
        for record in records
        if record.get("video_id")
    }
    publish_video_ids = {
        item["local"]["video_id"] for item in publish_actions
    }
    projected_rows: list[dict[str, Any]] = []
    publish_blocked_reasons: list[str] = []
    seen_published: list[str] = []
    for episode in episodes:
        compact = compact_episode(episode)
        if compact["status"] != "published":
            continue
        video_id = None
        for candidate, remote_items in episodes_by_video.items():
            if any(str(item.get("id")) == compact["episode_id"] for item in remote_items):
                video_id = candidate
                break
        record = record_by_video.get(video_id or "")
        if not video_id or not record or not record.get("upload_date"):
            publish_blocked_reasons.append("published_episode_missing_local_date")
            continue
        seen_published.append(video_id)
        projected_rows.append({
            "video_id": video_id,
            "date": record["upload_date"],
            "base_title": strip_episode_number(
                compact["title"] or record.get("title") or ""
            ),
            "episode_id": compact["episode_id"],
            "planned_publish": False,
            "current_number": compact["number"],
            "current_title": compact["title"],
        })
    if len(seen_published) != len(set(seen_published)):
        publish_blocked_reasons.append("duplicate_published_video_ids")

    for item in publish_actions:
        local = item["local"]
        projected_rows.append({
            "video_id": local["video_id"],
            "date": local["published_at"][:10].replace("-", ""),
            "base_title": local["base_title"],
            "episode_id": (
                (item.get("remote_precondition") or {}).get("episode_id")
                if item["action"] == "update_draft_then_publish"
                else None
            ),
            "planned_publish": True,
            "current_number": None,
            "current_title": None,
        })
    if len({row["video_id"] for row in projected_rows}) != len(projected_rows):
        publish_blocked_reasons.append("duplicate_video_ids_in_projected_feed")

    projected_feed: list[dict[str, Any]] = []
    projected_reorder_actions: list[dict[str, Any]] = []
    if not publish_blocked_reasons:
        projected_rows.sort(key=lambda row: (row["date"], row["video_id"]))
        for target_number, row in enumerate(projected_rows, 1):
            target_title = numbered_title(row["base_title"], target_number)
            target_row = {
                **row,
                "target_number": target_number,
                "target_title": target_title,
            }
            projected_feed.append(target_row)
            if (
                not row["planned_publish"]
                and row["current_number"] == target_number
                and row["current_title"] == target_title
            ):
                continue
            projected_reorder_actions.append(target_row)
    publish_blocked_reasons = sorted(set(publish_blocked_reasons))

    publish_scope = {
        "kind": "podcast_publish",
        "show_id": show_id,
        "youtube_snapshot": youtube_snapshot,
        "items": publish_actions,
        "projected_feed": projected_feed,
        "projected_reorder_actions": projected_reorder_actions,
        "publish_blocked_reasons": publish_blocked_reasons,
    }
    transcript_scope = {
        "kind": "transcript_backfill",
        "show_id": show_id,
        "items": transcript_actions,
    }
    description_scope = {
        "kind": "podcast_description_update",
        "show_id": show_id,
        "items": description_actions,
    }
    plan: dict[str, Any] = {
        "schema_version": 2,
        "kind": "kedaibiao_podcast_sync_plan",
        "generated_at": utc_now(),
        "show_id": show_id,
        "youtube_snapshot": youtube_snapshot,
        "remote_episode_count": len(episodes),
        "remote_by_status": dict(Counter(
            episode.get("attributes", {}).get("status") or "unknown"
            for episode in episodes
        )),
        "publish_actions": publish_actions,
        "candidate_publish_actions": candidate_publish_actions,
        "projected_published_episode_count": len(projected_rows),
        "projected_feed": projected_feed,
        "projected_reorder_actions": projected_reorder_actions,
        "publish_blocked_reasons": publish_blocked_reasons,
        "description_actions": description_actions,
        "transcript_actions": transcript_actions,
        "blocked": blocked,
        "publish_approval_hash": sha256_text(canonical_json(publish_scope)),
        "description_approval_hash": sha256_text(canonical_json(description_scope)),
        "transcript_approval_hash": sha256_text(canonical_json(transcript_scope)),
    }
    plan["plan_hash"] = plan_hash(plan)
    return plan


def write_summary(path: Path, plan: dict[str, Any]) -> None:
    lines = [
        "# Podcast sync approval plan",
        "",
        f"- Generated: `{plan['generated_at']}`",
        f"- Plan hash: `{plan['plan_hash']}`",
        f"- Transistor show: `{plan['show_id']}`",
        f"- Remote episodes: `{plan['remote_episode_count']}` `{json.dumps(plan['remote_by_status'], ensure_ascii=False)}`",
        f"- YouTube snapshot fresh: `{plan['youtube_snapshot']['fresh']}` (age `{plan['youtube_snapshot']['age_hours']}`h)",
        f"- Publication candidates: `{len(plan['candidate_publish_actions'])}`",
        f"- Publish actions: `{len(plan['publish_actions'])}`",
        f"- Projected reorder actions: `{len(plan['projected_reorder_actions'])}`",
        f"- Publish blocked reasons: `{plan['publish_blocked_reasons']}`",
        f"- Podcast description actions: `{len(plan['description_actions'])}`",
        f"- Transcript actions: `{len(plan['transcript_actions'])}`",
        f"- Blocked records: `{len(plan['blocked'])}`",
        f"- Publish approval hash: `{plan['publish_approval_hash']}`",
        f"- Description approval hash: `{plan['description_approval_hash']}`",
        f"- Transcript approval hash: `{plan['transcript_approval_hash']}`",
        "",
        "## Publish payload",
        "",
        "| action | date | video_id | description | transcript chars | warnings | title |",
        "|---|---:|---|---|---:|---|---|",
    ]
    for item in plan["publish_actions"]:
        local = item["local"]
        lines.append(
            f"| {item['action']} | {local['published_at'][:10]} | `{local['video_id']}` | "
            f"{local['description_chars']} chars / {local['description_source']} | "
            f"{local['transcript_chars']} | {','.join(item['warnings']) or 'none'} | "
            f"{local['base_title'].replace('|', '\\|')[:100]} |"
        )
    lines += [
        "",
        "## Projected public-feed reorder",
        "",
        "| video_id | planned publish | old number | target number | target title |",
        "|---|---|---:|---:|---|",
    ]
    for item in plan["projected_reorder_actions"]:
        lines.append(
            f"| `{item['video_id']}` | {item['planned_publish']} | "
            f"{item['current_number'] if item['current_number'] is not None else ''} | "
            f"{item['target_number']} | {item['target_title'].replace('|', '\\|')[:100]} |"
        )
    lines += [
        "",
        "## Podcast show-notes update payload",
        "",
        "| episode_id | status | video_id | old chars | new chars | source | title |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for item in plan["description_actions"]:
        lines.append(
            f"| `{item['episode_id']}` | {item['episode_status']} | `{item['video_id']}` | "
            f"{item['remote_description_chars']} | {item['description_chars']} | "
            f"`{item['description_path']}` | "
            f"{(item['episode_title'] or '').replace('|', '\\|')[:100]} |"
        )
    lines += [
        "",
        "## Transcript backfill payload",
        "",
        "| episode_id | status | video_id | chars | source | title |",
        "|---|---|---|---:|---|---|",
    ]
    for item in plan["transcript_actions"]:
        lines.append(
            f"| `{item['episode_id']}` | {item['episode_status']} | `{item['video_id']}` | "
            f"{item['transcript_chars']} | `{item['transcript_path']}` | "
            f"{(item['episode_title'] or '').replace('|', '\\|')[:100]} |"
        )
    lines += [
        "",
        "## Blocked",
        "",
        "| scope | video_id | reasons | title |",
        "|---|---|---|---|",
    ]
    for item in plan["blocked"]:
        lines.append(
            f"| {item['scope']} | `{item['video_id']}` | `{','.join(item['reasons'])}` | "
            f"{item['title'].replace('|', '\\|')[:100]} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--max-youtube-snapshot-age-hours", type=float, default=72)
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args()

    plan = build_plan(
        args.max_youtube_snapshot_age_hours,
        manifest_out_dir=PROJECT_ROOT / "logs" / "library_manifest",
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = args.out_dir / f"sync-plan-{stamp}.json"
    summary_path = args.out_dir / f"sync-plan-{stamp}.md"
    atomic_write_json(plan_path, plan)
    atomic_write_json(args.out_dir / "latest.json", plan)
    write_summary(summary_path, plan)
    (args.out_dir / "latest.md").write_text(
        summary_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    summary = {
        "plan_path": str(plan_path),
        "summary_path": str(summary_path),
        "plan_hash": plan["plan_hash"],
        "publish_candidate_count": len(plan["candidate_publish_actions"]),
        "publish_count": len(plan["publish_actions"]),
        "projected_reorder_count": len(plan["projected_reorder_actions"]),
        "publish_blocked_reasons": plan["publish_blocked_reasons"],
        "description_count": len(plan["description_actions"]),
        "transcript_count": len(plan["transcript_actions"]),
        "blocked_count": len(plan["blocked"]),
        "youtube_snapshot_fresh": plan["youtube_snapshot"]["fresh"],
        "publish_approval_hash": plan["publish_approval_hash"],
        "description_approval_hash": plan["description_approval_hash"],
        "transcript_approval_hash": plan["transcript_approval_hash"],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
