#!/usr/bin/env python3
"""Apply an immutable podcast sync plan after exact hash approval.

No remote write occurs unless an apply flag and its matching approval hash are
both supplied. The plan is re-hashed and all local source hashes are checked
again immediately before use.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.podcast.core import (  # noqa: E402
    atomic_write_json,
    canonical_json,
    episode_video_id,
    load_env,
    plan_hash,
    require_transistor_config,
    resolve_project_path,
    sha256_file,
    sha256_text,
    timed_text_to_text,
    utc_now,
)
from tools.podcast.transistor_client import (  # noqa: E402
    TransistorClient,
    TransistorError,
)
from tools.podcast.show_notes import (  # noqa: E402
    PORTABLE_HTML_FORMAT,
    render_portable_show_notes_html,
)


DEFAULT_PLAN = PROJECT_ROOT / "logs" / "podcast_sync" / "plans" / "latest.json"
LEDGER_DIR = PROJECT_ROOT / "logs" / "podcast_sync" / "ledgers"


class PlanPreconditionError(RuntimeError):
    pass


class Ledger:
    def __init__(self, plan: dict[str, Any], operation: str) -> None:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LEDGER_DIR / f"{operation}-{stamp}.jsonl"
        self.plan = plan
        self.operation = operation

    def write(self, event: str, **data: Any) -> None:
        item = {
            "timestamp": utc_now(),
            "event": event,
            "operation": self.operation,
            "plan_hash": self.plan["plan_hash"],
            **data,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(item) + "\n")
            handle.flush()
        print(json.dumps(item, ensure_ascii=False), flush=True)


def load_and_verify_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    expected = plan.get("plan_hash")
    actual = plan_hash(plan)
    if not expected or expected != actual:
        raise PlanPreconditionError(
            f"Plan hash mismatch: embedded={expected!r} actual={actual!r}"
        )
    if plan.get("kind") != "kedaibiao_podcast_sync_plan":
        raise PlanPreconditionError(f"Unexpected plan kind: {plan.get('kind')!r}")
    return plan


def verify_approval(plan: dict[str, Any], scope: str, supplied: str | None) -> None:
    key = f"{scope}_approval_hash"
    if scope == "transcript":
        approval_scope = {
            "kind": "transcript_backfill",
            "show_id": plan.get("show_id"),
            "items": plan.get("transcript_actions", []),
        }
    elif scope == "description":
        approval_scope = {
            "kind": "podcast_description_update",
            "show_id": plan.get("show_id"),
            "items": plan.get("description_actions", []),
        }
    elif scope == "publish":
        approval_scope = {
            "kind": "podcast_publish",
            "show_id": plan.get("show_id"),
            "youtube_snapshot": plan.get("youtube_snapshot"),
            "items": plan.get("publish_actions", []),
            "projected_feed": plan.get("projected_feed", []),
            "projected_reorder_actions": plan.get("projected_reorder_actions", []),
            "publish_blocked_reasons": plan.get("publish_blocked_reasons", []),
        }
    else:
        raise PlanPreconditionError(f"Unknown approval scope: {scope}")
    actual = sha256_text(canonical_json(approval_scope))
    expected = plan.get(key)
    if expected != actual:
        raise PlanPreconditionError(
            f"Embedded {scope} approval hash is invalid: embedded={expected} actual={actual}"
        )
    if not supplied:
        raise PlanPreconditionError(f"--approval-hash is required; expected {key}={actual}")
    if supplied != actual:
        raise PlanPreconditionError(
            f"Approval hash does not match {scope} payload: expected {actual}, got {supplied}"
        )


def read_transcript(path_value: str, expected_hash: str) -> str:
    path = resolve_project_path(path_value)
    if not path or not path.exists():
        raise PlanPreconditionError(f"Transcript source is missing: {path_value}")
    transcript = timed_text_to_text(path)
    actual = sha256_text(transcript)
    if actual != expected_hash:
        raise PlanPreconditionError(
            f"Transcript changed since approval: {path_value} expected={expected_hash} actual={actual}"
        )
    return transcript


def read_description(
    path_value: str | None,
    expected_hash: str,
    inline_text: str | None = None,
    source_sha256: str | None = None,
    description_format: str | None = None,
) -> str:
    if inline_text is not None:
        if path_value:
            raise PlanPreconditionError(
                "Description plan must use either a source path or inline text"
            )
        source = inline_text.strip()
        if source_sha256 and sha256_text(source) != source_sha256:
            raise PlanPreconditionError(
                "Inline description source changed since approval"
            )
        if description_format == PORTABLE_HTML_FORMAT:
            if not source_sha256:
                raise PlanPreconditionError(
                    "Portable HTML descriptions require a source hash"
                )
            description = render_portable_show_notes_html(source)
        elif description_format:
            raise PlanPreconditionError(
                f"Unsupported description format: {description_format}"
            )
        elif source_sha256:
            raise PlanPreconditionError(
                "Inline source hash requires a declared renderer"
            )
        else:
            description = source
    elif not path_value:
        description = ""
    else:
        path = resolve_project_path(path_value)
        if not path or not path.exists():
            raise PlanPreconditionError(f"Description source is missing: {path_value}")
        source = path.read_text(encoding="utf-8", errors="replace").strip()
        if source_sha256:
            observed_source_hash = sha256_text(source)
            if observed_source_hash != source_sha256:
                raise PlanPreconditionError(
                    "Description source changed since approval: "
                    f"{path_value} expected={source_sha256} "
                    f"actual={observed_source_hash}"
                )
        if description_format == PORTABLE_HTML_FORMAT:
            if not source_sha256:
                raise PlanPreconditionError(
                    "Portable HTML descriptions require a source hash"
                )
            description = render_portable_show_notes_html(source)
        elif description_format:
            raise PlanPreconditionError(
                f"Unsupported description format: {description_format}"
            )
        else:
            description = source
    actual = sha256_text(description)
    if actual != expected_hash:
        raise PlanPreconditionError(
            f"Description changed since approval: {path_value} expected={expected_hash} actual={actual}"
        )
    return description


def verify_audio(path_value: str | None, expected_hash: str | None) -> Path:
    path = resolve_project_path(path_value)
    if not path or not path.exists():
        raise PlanPreconditionError(f"Audio source is missing: {path_value}")
    actual = sha256_file(path)
    if not expected_hash or actual != expected_hash:
        raise PlanPreconditionError(
            f"Audio changed since approval: {path_value} expected={expected_hash} actual={actual}"
        )
    return path


def verify_episode_video(episode: dict[str, Any], video_id: str) -> dict[str, Any]:
    attrs = episode.get("attributes", {})
    try:
        observed = episode_video_id(attrs)
    except ValueError as exc:
        raise PlanPreconditionError(f"Episode {episode.get('id')} has conflicting YouTube identity") from exc
    if observed != video_id:
        raise PlanPreconditionError(
            f"Episode {episode.get('id')} video changed: expected={video_id} observed={observed}"
        )
    return attrs


def verify_transcript_readback(
    client: TransistorClient,
    episode_id: str,
    video_id: str,
    expected_text: str,
    *,
    attempts: int = 6,
) -> tuple[dict[str, Any], str]:
    """Verify exact text when exposed; otherwise verify Transistor's transcript artifact.

    Transistor accepts `transcript_text`, but its Episode resource normally
    exposes only `transcript_url` and generated format URLs. In that documented
    response shape an exact content hash cannot be read back through the public
    API, so the strongest available check is the presence of the transcript
    artifact after a successful PATCH.
    """
    expected_hash = sha256_text(expected_text)
    last: dict[str, Any] = {}
    for attempt in range(attempts):
        last = client.get_episode(episode_id)
        attrs = verify_episode_video(last, video_id)
        observed_text = attrs.get("transcript_text")
        if observed_text is not None and sha256_text(observed_text or "") == expected_hash:
            return last, "exact_text_hash"
        if attrs.get("transcript_url") or attrs.get("transcripts"):
            return last, "transcript_artifact_present"
        if attempt + 1 < attempts:
            time.sleep(2)
    raise PlanPreconditionError(
        f"Transcript artifact did not appear for episode {episode_id}"
    )


def apply_transcripts(
    client: TransistorClient,
    plan: dict[str, Any],
    ledger: Ledger,
) -> tuple[int, int]:
    updated = skipped = 0
    for item in plan["transcript_actions"]:
        episode_id = str(item["episode_id"])
        video_id = item["video_id"]
        transcript = read_transcript(item["transcript_path"], item["transcript_sha256"])
        episode = client.get_episode(episode_id)
        attrs = verify_episode_video(episode, video_id)
        current = attrs.get("transcript_text") or ""
        if sha256_text(current) == item["transcript_sha256"]:
            skipped += 1
            ledger.write(
                "transcript_already_current",
                episode_id=episode_id,
                video_id=video_id,
                transcript_sha256=item["transcript_sha256"],
            )
            continue
        if attrs.get("transcript_url") or attrs.get("transcripts"):
            skipped += 1
            ledger.write(
                "transcript_already_present_unverifiable",
                episode_id=episode_id,
                video_id=video_id,
                transcript_url=attrs.get("transcript_url"),
            )
            continue
        client.update_episode(episode_id, {"transcript_text": transcript})
        readback, verification = verify_transcript_readback(
            client,
            episode_id,
            video_id,
            transcript,
        )
        readback_attrs = readback.get("attributes", {})
        updated += 1
        ledger.write(
            "transcript_updated",
            episode_id=episode_id,
            video_id=video_id,
            transcript_sha256=item["transcript_sha256"],
            transcript_chars=len(transcript),
            verification=verification,
            transcript_url=readback_attrs.get("transcript_url"),
        )
    return updated, skipped


def apply_descriptions(
    client: TransistorClient,
    plan: dict[str, Any],
    ledger: Ledger,
) -> tuple[int, int]:
    """Update only explicitly sidecar-backed show notes on exact episode IDs."""
    updated = skipped = 0
    for item in plan.get("description_actions", []):
        episode_id = str(item["episode_id"])
        video_id = item["video_id"]
        description = read_description(
            item.get("description_path"),
            item["description_sha256"],
            source_sha256=item.get("description_source_sha256"),
            description_format=item.get("description_format"),
        )
        episode = client.get_episode(episode_id)
        attrs = verify_episode_video(episode, video_id)
        if attrs.get("status") != item.get("episode_status"):
            raise PlanPreconditionError(
                f"Episode {episode_id} status changed: "
                f"expected={item.get('episode_status')} observed={attrs.get('status')}"
            )

        current_raw = str(attrs.get("description") or "")
        current = current_raw.strip()
        current_hash = sha256_text(current)
        if current_hash == item["description_sha256"]:
            skipped += 1
            ledger.write(
                "description_already_current",
                episode_id=episode_id,
                video_id=video_id,
                description_sha256=item["description_sha256"],
            )
            continue
        if current_hash != item.get("remote_description_sha256"):
            raise PlanPreconditionError(
                f"Episode {episode_id} description changed since planning: "
                f"expected={item.get('remote_description_sha256')} observed={current_hash}"
            )

        client.update_episode(episode_id, {"description": description})
        dynamic_tags = re.findall(r"\{\{[^{}]+\}\}", description)
        try:
            readback = client.get_episode(episode_id)
            readback_attrs = verify_episode_video(readback, video_id)
            observed = str(readback_attrs.get("description") or "").strip()
            if observed != description:
                raise PlanPreconditionError(
                    f"Episode {episode_id} description readback mismatch"
                )
            if dynamic_tags:
                formatted = readback_attrs.get("formatted_description")
                if not isinstance(formatted, str):
                    raise PlanPreconditionError(
                        f"Episode {episode_id} did not expose formatted_description "
                        "for dynamic-tag verification"
                    )
                unexpanded = re.findall(r"\{\{[^{}]+\}\}", formatted)
                if unexpanded:
                    raise PlanPreconditionError(
                        f"Episode {episode_id} left dynamic Show Notes tags unexpanded: "
                        f"{unexpanded}"
                    )
        except PlanPreconditionError as exc:
            client.update_episode(episode_id, {"description": current_raw})
            rollback = client.get_episode(episode_id)
            rollback_attrs = verify_episode_video(rollback, video_id)
            if str(rollback_attrs.get("description") or "") != current_raw:
                raise PlanPreconditionError(
                    f"Episode {episode_id} failed verification and rollback readback"
                ) from exc
            ledger.write(
                "description_rolled_back",
                episode_id=episode_id,
                video_id=video_id,
                reason=str(exc),
                restored_description_sha256=sha256_text(current),
            )
            raise
        updated += 1
        ledger.write(
            "description_updated",
            episode_id=episode_id,
            video_id=video_id,
            description_sha256=item["description_sha256"],
            description_chars=len(description),
            verification="exact_text_hash",
            dynamic_tags_verified=bool(dynamic_tags),
        )
    return updated, skipped


def desired_episode_fields(local: dict[str, Any]) -> dict[str, Any]:
    description = read_description(
        local.get("description_path"),
        local["description_sha256"],
        local.get("description_text"),
        source_sha256=local.get("description_source_sha256"),
        description_format=local.get("description_format"),
    )
    desired: dict[str, Any] = {
        "title": local["base_title"],
        "description": description,
        "youtube_url": local["video_url"],
        "image_url": local["image_url"],
    }
    if local.get("transcript_path"):
        desired["transcript_text"] = read_transcript(
            local["transcript_path"],
            local["transcript_sha256"],
        )
    return desired


def readback_matches(
    episode: dict[str, Any],
    video_id: str,
    desired: dict[str, Any],
) -> None:
    attrs = verify_episode_video(episode, video_id)
    for key in ("title", "description"):
        if (attrs.get(key) or "") != (desired.get(key) or ""):
            raise PlanPreconditionError(
                f"Episode {episode.get('id')} readback mismatch for {key}"
            )
    if "number" in desired and (
        type(attrs.get("number")) is not int or attrs["number"] != desired["number"]
    ):
        raise PlanPreconditionError(
            f"Episode {episode.get('id')} readback mismatch for number"
        )


def verify_published_feed_preconditions(
    episodes: list[dict[str, Any]],
    projected_feed: list[dict[str, Any]],
) -> None:
    """Check the entire historical feed before the first upload or API write."""
    expected: dict[str, dict[str, Any]] = {}
    for row in projected_feed:
        if row.get("planned_publish"):
            continue
        video_id = row.get("video_id")
        if not video_id or video_id in expected:
            raise PlanPreconditionError("Frozen historical feed contains missing or duplicate identities")
        expected[video_id] = row

    observed: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        attrs = episode.get("attributes", {})
        if attrs.get("status") != "published":
            continue
        try:
            video_id = episode_video_id(attrs)
        except ValueError as exc:
            raise PlanPreconditionError("Published feed has conflicting YouTube identities") from exc
        if not video_id or video_id in observed:
            raise PlanPreconditionError("Published feed contains missing or duplicate YouTube identities")
        observed[video_id] = episode
    if set(observed) != set(expected):
        raise PlanPreconditionError("Published feed identities changed since planning; rebuild the plan")
    for video_id, row in expected.items():
        episode = observed[video_id]
        attrs = episode.get("attributes", {})
        if (
            str(episode.get("id") or "") != str(row.get("episode_id") or "")
            or attrs.get("number") != row.get("current_number")
            or (attrs.get("title") or "") != row.get("current_title")
        ):
            raise PlanPreconditionError(
                f"Historical episode {video_id} changed since planning; rebuild the plan"
            )


def apply_publish(
    client: TransistorClient,
    show_id: str,
    plan: dict[str, Any],
    ledger: Ledger,
) -> tuple[int, int]:
    if not plan.get("youtube_snapshot", {}).get("fresh"):
        raise PlanPreconditionError("Publish plan is blocked by a stale YouTube snapshot")
    if plan.get("publish_blocked_reasons"):
        raise PlanPreconditionError(
            f"Publish plan is blocked: {plan['publish_blocked_reasons']}"
        )

    publish_ids = {item["local"]["video_id"] for item in plan["publish_actions"]}
    frozen_titles: dict[str, str] = {}
    frozen_numbers: dict[str, int] = {}
    seen_numbers: set[int] = set()
    for row in plan.get("projected_feed", []):
        video_id = row.get("video_id")
        number = row.get("target_number")
        if type(number) is not int or number <= 0 or number in seen_numbers:
            raise PlanPreconditionError(
                f"Invalid or duplicate frozen episode number for {video_id}"
            )
        seen_numbers.add(number)
        if video_id not in publish_ids:
            continue
        title = row.get("target_title")
        if (
            video_id in frozen_titles
            or not row.get("planned_publish")
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise PlanPreconditionError(
                f"Invalid or duplicate frozen publish title for {video_id}"
            )
        frozen_titles[video_id] = title
        frozen_numbers[video_id] = number
    missing_titles = publish_ids - frozen_titles.keys()
    if missing_titles:
        raise PlanPreconditionError(
            f"Missing frozen publish titles in projected_feed: {sorted(missing_titles)}"
        )

    current_episodes, current_by_video = client.episodes_by_video_id(show_id)
    verify_published_feed_preconditions(current_episodes, plan.get("projected_feed", []))
    published = repaired = 0
    for item in plan["publish_actions"]:
        local = item["local"]
        video_id = local["video_id"]
        desired = desired_episode_fields(local)
        desired["title"] = frozen_titles[video_id]
        desired["number"] = frozen_numbers[video_id]
        current = current_by_video.get(video_id, [])

        if item["action"] == "create_draft_then_publish":
            if current:
                raise PlanPreconditionError(
                    f"Remote precondition changed for {video_id}: expected absent, found {len(current)}"
                )
            audio = verify_audio(local.get("audio_path"), local.get("audio_sha256"))
            authorization = client.authorize_upload(audio.name)
            client.upload_audio(
                authorization["upload_url"],
                authorization["content_type"],
                audio,
            )
            create_payload = {
                "show_id": show_id,
                "audio_url": authorization["audio_url"],
                **desired,
            }
            episode = client.create_episode(create_payload)
            episode_id = str(episode.get("id") or "")
            if not episode_id:
                raise TransistorError(f"Create response for {video_id} had no episode id")
            episode = client.update_episode(episode_id, desired)
            ledger.write(
                "draft_created",
                episode_id=episode_id,
                video_id=video_id,
                number=desired["number"],
            )
        elif item["action"] == "update_draft_then_publish":
            if len(current) != 1:
                raise PlanPreconditionError(
                    f"Remote precondition changed for {video_id}: expected one draft, found {len(current)}"
                )
            episode = current[0]
            episode_id = str(episode.get("id") or "")
            attrs = verify_episode_video(episode, video_id)
            if attrs.get("status") != "draft":
                raise PlanPreconditionError(
                    f"Episode {episode_id} is no longer draft: {attrs.get('status')}"
                )
            expected_id = str((item.get("remote_precondition") or {}).get("episode_id") or "")
            if expected_id and episode_id != expected_id:
                raise PlanPreconditionError(
                    f"Draft identity changed for {video_id}: expected={expected_id} observed={episode_id}"
                )
            if not attrs.get("media_url"):
                audio = verify_audio(local.get("audio_path"), local.get("audio_sha256"))
                authorization = client.authorize_upload(audio.name)
                client.upload_audio(
                    authorization["upload_url"],
                    authorization["content_type"],
                    audio,
                )
                desired["audio_url"] = authorization["audio_url"]
            episode = client.update_episode(episode_id, desired)
            repaired += 1
            ledger.write(
                "draft_repaired",
                episode_id=episode_id,
                video_id=video_id,
                number=desired["number"],
            )
        else:
            raise PlanPreconditionError(f"Unknown publish action: {item['action']}")

        readback = client.get_episode(episode_id)
        readback_matches(readback, video_id, desired)
        if "transcript_text" in desired:
            _, transcript_verification = verify_transcript_readback(
                client,
                episode_id,
                video_id,
                desired["transcript_text"],
            )
        else:
            transcript_verification = "not_requested"
        client.publish_episode(episode_id, local["published_at"])
        final = client.get_episode(episode_id)
        readback_matches(final, video_id, desired)
        final_attrs = verify_episode_video(final, video_id)
        if final_attrs.get("status") != "published":
            raise PlanPreconditionError(
                f"Episode {episode_id} did not become published: {final_attrs.get('status')}"
            )
        expected_date = local["published_at"][:10]
        observed_date = (final_attrs.get("published_at") or "")[:10]
        if observed_date != expected_date:
            raise PlanPreconditionError(
                f"Episode {episode_id} published_at mismatch: "
                f"expected={expected_date} observed={observed_date}"
            )
        published += 1
        ledger.write(
            "episode_published",
            episode_id=episode_id,
            video_id=video_id,
            published_at=final_attrs.get("published_at"),
            transcript_sha256=local.get("transcript_sha256"),
            transcript_verification=transcript_verification,
        )

    apply_projected_reorder(client, show_id, plan, ledger)
    return published, repaired


def apply_projected_reorder(
    client: TransistorClient,
    show_id: str,
    plan: dict[str, Any],
    ledger: Ledger,
) -> None:
    episodes = [
        episode
        for episode in client.list_episodes(show_id)
        if episode.get("attributes", {}).get("status") == "published"
    ]
    by_video: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        attrs = episode.get("attributes", {})
        video_id = episode_video_id(attrs)
        if video_id:
            by_video.setdefault(video_id, []).append(episode)
    projected_feed = plan.get("projected_feed", [])
    if len(episodes) != len(projected_feed):
        raise PlanPreconditionError(
            f"Projected feed count changed: expected={len(projected_feed)} observed={len(episodes)}"
        )
    if set(by_video) != {item["video_id"] for item in projected_feed}:
        raise PlanPreconditionError("Projected feed video-id set changed")
    if any(len(items) != 1 for items in by_video.values()):
        raise PlanPreconditionError("Projected feed contains duplicate published video ids")

    # Validate every row before the first renumber write.
    for row in projected_feed:
        episode = by_video[row["video_id"]][0]
        attrs = episode.get("attributes", {})
        current_is_target = (
            attrs.get("number") == row["target_number"]
            and (attrs.get("title") or "") == row["target_title"]
        )
        if row["planned_publish"]:
            if not current_is_target and attrs.get("status") != "published":
                raise PlanPreconditionError(
                    f"Projected episode {episode.get('id')} is not published"
                )
        elif not current_is_target:
            if (
                attrs.get("number") != row["current_number"]
                or (attrs.get("title") or "") != row["current_title"]
            ):
                raise PlanPreconditionError(
                    f"Reorder precondition changed for episode {episode.get('id')}"
                )

    updated = skipped = 0
    for row in plan.get("projected_reorder_actions", []):
        episode = by_video[row["video_id"]][0]
        episode_id = str(episode.get("id") or "")
        attrs = episode.get("attributes", {})
        if (
            attrs.get("number") == row["target_number"]
            and (attrs.get("title") or "") == row["target_title"]
        ):
            skipped += 1
            continue
        client.update_episode(
            episode_id,
            {
                "number": row["target_number"],
                "title": row["target_title"],
            },
        )
        readback = client.get_episode(episode_id)
        readback_attrs = verify_episode_video(readback, row["video_id"])
        if (
            readback_attrs.get("number") != row["target_number"]
            or (readback_attrs.get("title") or "") != row["target_title"]
        ):
            raise PlanPreconditionError(
                f"Projected reorder readback failed for episode {episode_id}"
            )
        updated += 1
        ledger.write(
            "episode_reordered",
            episode_id=episode_id,
            video_id=row["video_id"],
            target_number=row["target_number"],
            target_title=row["target_title"],
        )
    ledger.write(
        "projected_reorder_completed",
        updated=updated,
        skipped=skipped,
        published_episode_count=len(episodes),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--apply-descriptions", action="store_true")
    scope.add_argument("--apply-transcripts", action="store_true")
    scope.add_argument("--prepare-and-publish", action="store_true")
    parser.add_argument("--approval-hash")
    args = parser.parse_args()

    plan = load_and_verify_plan(args.plan)
    if args.apply_descriptions:
        operation = "description"
    elif args.apply_transcripts:
        operation = "transcript"
    else:
        operation = "publish"
    verify_approval(plan, operation, args.approval_hash)
    load_env()
    api_key, show_id = require_transistor_config()
    if str(show_id) != str(plan.get("show_id")):
        raise PlanPreconditionError(
            f"Configured show id {show_id} does not match approved plan {plan.get('show_id')}"
        )

    client = TransistorClient(api_key)
    ledger = Ledger(plan, operation)
    ledger.write("execution_started", plan_path=str(args.plan.resolve()))
    try:
        if args.apply_descriptions:
            updated, skipped = apply_descriptions(client, plan, ledger)
            ledger.write("execution_completed", updated=updated, skipped=skipped)
        elif args.apply_transcripts:
            updated, skipped = apply_transcripts(client, plan, ledger)
            ledger.write("execution_completed", updated=updated, skipped=skipped)
        else:
            published, repaired = apply_publish(client, show_id, plan, ledger)
            ledger.write("execution_completed", published=published, repaired_drafts=repaired)
    except Exception as exc:
        ledger.write("execution_failed", error_type=type(exc).__name__, error=str(exc))
        raise
    print(f"Ledger: {ledger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
