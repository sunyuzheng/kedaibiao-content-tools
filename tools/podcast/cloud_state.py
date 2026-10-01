"""Small private operational state and allowlisted immutable plan bundles."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from tools.podcast.core import atomic_write_json, sha256_file, utc_now

SHOW_ID = "71709"
CHANNEL_ID = "UC_5lJHgnMP_lb_VpIiXV0hQ"
OPS_REPO = "sunyuzheng/kedaibiao-podcast-ops"
INFO_FIELDS = ("id", "title", "upload_date", "availability", "live_status", "was_live", "is_live", "channel_id")
ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
PLAN_PATH = "logs/cloud/plan.json"
SNAPSHOTS = (
    "tools/youtube/public_videos_snapshot.json",
    "tools/youtube/podcast_candidate_verification.json",
)


class CloudBlocked(RuntimeError):
    """A deliberately safe, human-readable error; contains no service response."""


def minimal_info(info: dict) -> dict:
    result = {key: info[key] for key in INFO_FIELDS if key in info}
    if not ID.fullmatch(str(result.get("id", ""))):
        raise CloudBlocked("Invalid video ID in source metadata")
    date = str(result.get("upload_date", ""))
    if not re.fullmatch(r"\d{8}", date):
        raise CloudBlocked(f"Missing source date: {result['id']}")
    if not str(result.get("title") or "").strip():
        raise CloudBlocked(f"Missing source title: {result['id']}")
    return result


def validate_state(state: dict) -> dict:
    if (state.get("schema_version"), str(state.get("show_id")), state.get("channel_id")) != (1, SHOW_ID, CHANNEL_ID):
        raise CloudBlocked("State schema/show/channel mismatch; never initialize over existing state")
    if not isinstance(state.get("records"), dict) or not state["records"]:
        raise CloudBlocked("Historical metadata baseline is empty")
    for video_id, record in state["records"].items():
        if minimal_info(record)["id"] != video_id:
            raise CloudBlocked("State record identity mismatch")
    return state


def folder_for(root: Path, record: dict) -> Path:
    record = minimal_info(record)
    return root / "archive" / "无人工字幕" / f"{record['upload_date']}_{record['id']}"


def restore_metadata(root: Path, state: dict) -> None:
    for record in validate_state(state)["records"].values():
        atomic_write_json(folder_for(root, record) / "source.info.json", record)


def bootstrap(source: Path, ops: Path) -> dict:
    """Export only minimum source metadata, never OAuth/cookies/formats/media."""
    path = ops / "runtime/state.json"
    if path.exists():
        raise CloudBlocked("Refusing to overwrite operational state")
    records = {}
    skipped = 0
    for info_path in sorted((source / "archive").glob("*/*/*.info.json")):
        try:
            record = minimal_info(json.loads(info_path.read_text(encoding="utf-8")))
        except (CloudBlocked, ValueError):
            skipped += 1
            continue
        video_id = record["id"]
        previous = records.get(video_id)
        if previous and previous.get("upload_date") != record.get("upload_date"):
            raise CloudBlocked(f"Conflicting historical dates: {video_id}")
        records[video_id] = record
    state = validate_state({"schema_version": 1, "show_id": SHOW_ID, "channel_id": CHANNEL_ID,
                            "records": records, "created_at": utc_now(), "pending_execution": None})
    atomic_write_json(path, state)
    shutil.copyfile(source / "podcast_series.json", ops / "podcast_series.json")
    return {"records": len(records), "skipped_incomplete_metadata": skipped}


def allowed_bundle_path(value: str) -> bool:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        return False
    if value == PLAN_PATH or value in SNAPSHOTS:
        return True
    if len(path.parts) == 2 and path.parts[0] == "podcast_show_notes":
        return bool(ID.fullmatch(path.stem)) and path.suffix == ".txt"
    if len(path.parts) == 4 and path.parts[:2] == ("archive", "无人工字幕"):
        return bool(re.fullmatch(r"\d{8}_[A-Za-z0-9_-]{11}", path.parts[2])) and path.name in {
            "source.info.json", "source.description", "source.m4a", "source.mp3", "source.opus", "source.webm"
        }
    return False


def export_bundle(root: Path, destination: Path, *, code_sha: str, catalog: Path) -> dict:
    if not SHA.fullmatch(code_sha):
        raise CloudBlocked("Code must be an exact Git commit")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    paths = [root / PLAN_PATH, *(root / name for name in SNAPSHOTS)]
    paths.extend((root / "archive" / "无人工字幕").glob("*/*"))
    plan = json.loads((root / PLAN_PATH).read_text(encoding="utf-8"))
    candidate_ids = {row["local"]["video_id"] for row in plan.get("publish_actions", [])}
    paths.extend(root / "podcast_show_notes" / f"{video_id}.txt" for video_id in candidate_ids)
    files = {}
    for path in paths:
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        if not allowed_bundle_path(relative) or path.is_symlink():
            raise CloudBlocked("Unexpected file in cloud bundle")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        files[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {"schema_version": 1, "show_id": SHOW_ID, "channel_id": CHANNEL_ID,
                "code_sha": code_sha, "catalog_sha256": sha256_file(catalog),
                "plan_hash": plan["plan_hash"], "approval_hash": plan["publish_approval_hash"], "files": files}
    atomic_write_json(destination / "manifest.json", manifest)
    return manifest


def import_bundle(bundle: Path, root: Path, *, code_sha: str, catalog: Path,
                  plan_hash: str, approval_hash: str) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    expected = {"schema_version": 1, "show_id": SHOW_ID, "channel_id": CHANNEL_ID,
                "code_sha": code_sha, "catalog_sha256": sha256_file(catalog),
                "plan_hash": plan_hash, "approval_hash": approval_hash}
    if not plan_hash or not approval_hash or any(manifest.get(k) != v for k, v in expected.items()):
        raise CloudBlocked("Reviewed plan, code, catalog or destination changed; regenerate and review")
    files = manifest.get("files", {})
    if not all(name in files for name in (PLAN_PATH, *SNAPSHOTS)):
        raise CloudBlocked("Incomplete plan bundle")
    # Validate everything before copying anything; never extract archives or executable files.
    for name, metadata in files.items():
        source = bundle / name
        if (not allowed_bundle_path(name) or not source.is_file() or source.is_symlink()
                or not source.resolve().is_relative_to(bundle.resolve())
                or source.stat().st_size != metadata.get("bytes")
                or sha256_file(source) != metadata.get("sha256")):
            raise CloudBlocked("Bundle contains an unsafe, missing or modified file")
        target = root / name
        if not target.resolve().is_relative_to(root.resolve()):
            raise CloudBlocked("Bundle destination escaped project")
    reviewed_plan = json.loads((bundle / PLAN_PATH).read_text(encoding="utf-8"))
    if (reviewed_plan.get("plan_hash") != plan_hash
            or reviewed_plan.get("publish_approval_hash") != approval_hash):
        raise CloudBlocked("Bundle plan does not match the exact reviewed hashes")
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / name, target)
    return json.loads((root / PLAN_PATH).read_text(encoding="utf-8"))
