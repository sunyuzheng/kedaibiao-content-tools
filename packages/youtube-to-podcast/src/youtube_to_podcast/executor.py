"""Execution boundary for an exact, previously reviewed plan."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .hashing import PlanError, canonical_json, sha256_file, sha256_text, verify_plan
from .transcript import timed_text_to_text
from .transistor import TransistorClient, TransistorError, extract_video_id


class ExecutionError(PlanError):
    """Raised before or during a fail-closed remote execution."""


class Ledger:
    def __init__(self, config: Config, plan: dict[str, Any]) -> None:
        ledger_dir = config.work_dir / "ledgers"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = ledger_dir / f"apply-{stamp}.jsonl"
        self.plan_hash = plan["plan_hash"]

    def write(self, event: str, **data: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "plan_hash": self.plan_hash,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def _resolve_source(config: Config, value: str | None) -> Path | None:
    if not value:
        return None
    path = (config.root / value).resolve()
    path.relative_to(config.root.resolve())
    return path


def _verify_source(
    config: Config, action: dict[str, Any]
) -> tuple[Path, str | None]:
    audio = _resolve_source(config, action.get("audio_path"))
    if not audio or not audio.is_file():
        raise ExecutionError(f"Audio source is missing for {action['video_id']}")
    observed_audio_hash = sha256_file(audio)
    if observed_audio_hash != action.get("audio_sha256"):
        raise ExecutionError(
            f"Audio source changed for {action['video_id']}: "
            f"expected={action.get('audio_sha256')} observed={observed_audio_hash}"
        )

    transcript_text: str | None = None
    transcript = _resolve_source(config, action.get("transcript_path"))
    if transcript:
        if not transcript.is_file():
            raise ExecutionError(
                f"Transcript source is missing for {action['video_id']}"
            )
        transcript_text = timed_text_to_text(transcript)
        observed_transcript_hash = sha256_text(transcript_text)
        if observed_transcript_hash != action.get("transcript_sha256"):
            raise ExecutionError(
                f"Transcript source changed for {action['video_id']}"
            )
    if sha256_text(str(action.get("description") or "")) != action.get(
        "description_sha256"
    ):
        raise ExecutionError(f"Description changed for {action['video_id']}")
    return audio, transcript_text


def _verify_remote_precondition(
    action: dict[str, Any],
    current_by_video: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    video_id = action["video_id"]
    current = current_by_video.get(video_id, [])
    if action["action"] in {"create_draft", "create_and_publish"}:
        if current:
            raise ExecutionError(
                f"Remote precondition changed for {video_id}: "
                f"expected absent, found {len(current)}"
            )
        return current
    raise ExecutionError(f"Unsupported action: {action['action']}")


def _desired_fields(
    action: dict[str, Any],
    transcript_text: str | None,
) -> dict[str, Any]:
    desired: dict[str, Any] = {
        "title": action["title"],
        "description": action.get("description") or "",
        "video_url": action["video_url"],
        "image_url": action["image_url"],
    }
    if transcript_text:
        desired["transcript_text"] = transcript_text
    return desired


def _verify_readback(
    client: TransistorClient,
    episode_id: str,
    action: dict[str, Any],
    desired: dict[str, Any],
) -> dict[str, Any]:
    episode = client.get_episode(episode_id)
    attrs = episode.get("attributes") or {}
    observed_video_id = extract_video_id(attrs.get("video_url"))
    if observed_video_id != action["video_id"]:
        raise ExecutionError(
            f"Episode {episode_id} video mismatch: {observed_video_id}"
        )
    for key in ("title", "description"):
        if (attrs.get(key) or "") != (desired.get(key) or ""):
            raise ExecutionError(f"Episode {episode_id} {key} readback mismatch")
    if not attrs.get("media_url"):
        raise ExecutionError(f"Episode {episode_id} has no audio artifact")
    if desired.get("image_url") and not attrs.get("image_url"):
        raise ExecutionError(f"Episode {episode_id} has no thumbnail")
    return episode


def _verify_transcript_readback(
    client: TransistorClient,
    episode_id: str,
    action: dict[str, Any],
    expected_text: str,
    *,
    attempts: int = 6,
) -> str:
    """Verify exact transcript text, or the generated artifact when text is hidden."""
    expected_hash = sha256_text(expected_text)
    for attempt in range(attempts):
        episode = client.get_episode(episode_id)
        attrs = episode.get("attributes") or {}
        if extract_video_id(attrs.get("video_url")) != action["video_id"]:
            raise ExecutionError(
                f"Episode {episode_id} changed while verifying its transcript"
            )
        observed_text = attrs.get("transcript_text")
        if observed_text is not None:
            if sha256_text(observed_text or "") != expected_hash:
                raise ExecutionError(
                    f"Episode {episode_id} transcript readback mismatch"
                )
            return "exact_text_hash"
        if attrs.get("transcript_url") or attrs.get("transcripts"):
            return "transcript_artifact_present"
        if attempt + 1 < attempts:
            time.sleep(2)
    raise ExecutionError(
        f"Episode {episode_id} transcript artifact did not appear"
    )


def apply_plan(
    config: Config,
    client: TransistorClient,
    plan: dict[str, Any],
    *,
    supplied_approval_hash: str,
) -> dict[str, Any]:
    verify_plan(plan, supplied_approval_hash)
    if str(plan.get("show_id")) != config.show_id:
        raise ExecutionError("Configured show ID does not match the approved plan")
    if plan.get("channel_url") != config.channel_url:
        raise ExecutionError("Configured channel URL does not match the approved plan")
    if plan.get("mode") != config.policy.mode:
        raise ExecutionError("Configured mode does not match the approved plan")
    if plan.get("publication") != config.policy.publication:
        raise ExecutionError("Configured publication policy changed after planning")

    _, current_by_video = client.episodes_by_video_id(config.show_id)
    prepared: dict[str, tuple[Path, str | None, list[dict[str, Any]]]] = {}
    # Validate every source and every remote target before the first write.
    for action in plan.get("actions", []):
        audio, transcript_text = _verify_source(config, action)
        current = _verify_remote_precondition(action, current_by_video)
        prepared[action["video_id"]] = (audio, transcript_text, current)

    ledger = Ledger(config, plan)
    ledger.write(
        "execution_started",
        action_count=len(plan.get("actions", [])),
        publication=plan.get("publication"),
    )
    created = published = updated = 0
    last_created_number: int | None = None
    try:
        for action in plan.get("actions", []):
            video_id = action["video_id"]
            audio, transcript_text, current = prepared[video_id]
            desired = _desired_fields(action, transcript_text)
            action_name = action["action"]

            if action_name in {"create_draft", "create_and_publish"}:
                authorization = client.authorize_upload(audio.name)
                client.upload_audio(
                    authorization["upload_url"],
                    authorization["content_type"],
                    audio,
                )
                episode = client.create_episode(
                    {
                        "show_id": config.show_id,
                        "increment_number": True,
                        "audio_url": authorization["audio_url"],
                        **desired,
                    }
                )
                episode_id = str(episode.get("id") or "")
                if not episode_id:
                    raise TransistorError(
                        f"Create response for {video_id} has no episode ID"
                    )
                client.update_episode(episode_id, desired)
                created += 1
                ledger.write(
                    "draft_created",
                    episode_id=episode_id,
                    video_id=video_id,
                )
            else:
                raise ExecutionError(f"Unsupported action: {action_name}")

            readback = _verify_readback(
                client,
                episode_id,
                action,
                desired,
            )
            observed_number = (readback.get("attributes") or {}).get("number")
            try:
                episode_number = int(observed_number)
            except (TypeError, ValueError) as exc:
                raise ExecutionError(
                    f"Episode {episode_id} has no valid episode number"
                ) from exc
            if (
                last_created_number is not None
                and episode_number != last_created_number + 1
            ):
                raise ExecutionError(
                    f"Episode numbering is not sequential: "
                    f"previous={last_created_number} observed={episode_number}"
                )
            last_created_number = episode_number
            transcript_verification = (
                _verify_transcript_readback(
                    client,
                    episode_id,
                    action,
                    transcript_text,
                )
                if transcript_text
                else "not_requested"
            )
            ledger.write(
                "episode_verified",
                episode_id=episode_id,
                video_id=video_id,
                episode_number=episode_number,
                transcript_verification=transcript_verification,
            )
            readback_status = (readback.get("attributes") or {}).get("status")
            if action_name == "create_draft":
                if readback_status != "draft":
                    raise ExecutionError(
                        f"Episode {episode_id} is not a draft after creation"
                    )
                continue

            client.publish_episode(episode_id, action["published_at"])
            final = client.get_episode(episode_id)
            final_attrs = final.get("attributes") or {}
            if final_attrs.get("status") != "published":
                raise ExecutionError(
                    f"Episode {episode_id} did not become published"
                )
            expected_date = action["published_at"][:10]
            observed_date = str(final_attrs.get("published_at") or "")[:10]
            if observed_date != expected_date:
                raise ExecutionError(
                    f"Episode {episode_id} publish date mismatch: "
                    f"expected={expected_date} observed={observed_date}"
                )
            published += 1
            ledger.write(
                "episode_published",
                episode_id=episode_id,
                video_id=video_id,
                published_at=final_attrs.get("published_at"),
            )
    except Exception as exc:
        ledger.write(
            "execution_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:1000],
        )
        raise
    ledger.write(
        "execution_completed",
        created=created,
        updated=updated,
        published=published,
    )
    return {
        "created": created,
        "updated": updated,
        "published": published,
        "ledger_path": str(ledger.path),
    }
