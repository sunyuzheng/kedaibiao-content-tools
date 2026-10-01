#!/usr/bin/env python3
"""Private GitHub Actions entry point. Default: discover and build a reviewable plan."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.podcast.core import atomic_write_json, episode_video_id, require_transistor_config, sha256_text, utc_now
from tools.podcast.cloud_state import (
    CHANNEL_ID, SHOW_ID, OPS_REPO, PLAN_PATH, SNAPSHOTS, CloudBlocked,
    bootstrap, export_bundle, folder_for, import_bundle, minimal_info, restore_metadata, validate_state,
)
from tools.podcast.series import load_catalog
from tools.podcast.transistor_client import TransistorClient
from tools.automation.podcast_auto_publish import evaluate_auto_publish

MAX_ITEMS = 3
MAX_PROBES = 10
MAX_AGE_HOURS = 6


def command_json(command: list[str], *, cwd: Path = ROOT) -> dict:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=600)
    if result.returncode:
        raise CloudBlocked(f"Read-only command failed: {Path(command[0]).name}; exit {result.returncode}")
    return json.loads(result.stdout)


def guard_ops(ops: Path) -> None:
    """Recheck server visibility before state writes/publication, not just workflow UI."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != OPS_REPO:
        raise CloudBlocked("Publication/checkpoint requires the designated private GitHub Actions repository")
    repo = command_json(["gh", "api", f"repos/{OPS_REPO}"], cwd=ops)
    if repo.get("private") is not True or repo.get("full_name") != OPS_REPO or repo.get("fork"):
        raise CloudBlocked("Operational repository must be private, non-fork and owned by sunyuzheng")
    origin = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=ops, text=True).strip()
    if origin not in {f"https://github.com/{OPS_REPO}", f"https://github.com/{OPS_REPO}.git"}:
        raise CloudBlocked("Unexpected operational Git remote")
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ops, text=True).strip()
    if branch != "main":
        raise CloudBlocked("Operational state must be on main")


def checkpoint(ops: Path, state: dict) -> None:
    guard_ops(ops)
    atomic_write_json(ops / "runtime/state.json", state)
    # Never force push or rebase mutable state. Concurrent edits require a fresh run.
    commands = [
        ["git", "config", "user.name", "Yuzheng Sun"],
        ["git", "config", "user.email", "sunyuzheng@gmail.com"],
        ["git", "add", "--", "runtime/state.json"],
    ]
    for command in commands:
        subprocess.run(command, cwd=ops, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "runtime/state.json"], cwd=ops).returncode
    if changed == 1:
        subprocess.run(["git", "commit", "-m", "Record podcast reconciliation state", "--", "runtime/state.json"],
                       cwd=ops, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif changed:
        raise CloudBlocked("Cannot inspect operational state changes")
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=ops, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def select_candidates(videos: list[dict], episodes: list[dict]) -> list[dict]:
    ids = [row.get("video_id") for row in videos]
    if not ids or len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(x)) for x in ids):
        raise CloudBlocked("YouTube listing is empty or has invalid/duplicate IDs")
    positions = [row.get("playlist_index") for row in videos]
    if any(type(p) is not int or p < 1 for p in positions) or positions != sorted(set(positions)):
        raise CloudBlocked("YouTube listing order is missing or ambiguous")
    published = set()
    for episode in episodes:
        attrs = episode.get("attributes", {})
        video_id = episode_video_id(attrs)
        if attrs.get("status") == "published":
            if not video_id or video_id in published:
                raise CloudBlocked("Published Transistor feed has an unidentified or duplicate YouTube association")
            published.add(video_id)
    baseline_positions = [row["playlist_index"] for row in videos if row["video_id"] in published]
    if not baseline_positions:
        raise CloudBlocked("No published playlist baseline; historical backfill needs separate review")
    baseline = min(baseline_positions)
    candidates = [row for row in videos if row["playlist_index"] < baseline and row["video_id"] not in published]
    if len(candidates) > MAX_PROBES:
        raise CloudBlocked("More than 10 entries ahead of published baseline; inspect backlog before downloading")
    for candidate in candidates:
        matches = [episode for episode in episodes if episode_video_id(episode.get("attributes", {})) == candidate["video_id"]]
        if len(matches) > 1:
            raise CloudBlocked(f"Multiple remote drafts for new candidate: {candidate['video_id']}")
    return candidates


def verify_probe(video_id: str, info: dict) -> str:
    if info.get("id") != video_id or info.get("channel_id") != CHANNEL_ID:
        raise CloudBlocked(f"YouTube probe identity/channel mismatch: {video_id}")
    if info.get("availability") in {"subscriber_only", "premium_only", "needs_auth"}:
        return "excluded_member"
    if info.get("is_live") or info.get("was_live") or info.get("live_status") in {"is_live", "is_upcoming", "was_live", "post_live"}:
        return "excluded_live"
    if info.get("availability") != "public" or info.get("live_status") not in {None, "not_live"}:
        raise CloudBlocked(f"Public normal-video status could not be verified: {video_id}")
    minimal_info(info)
    return "public"


def classify_youtube_error(stderr: str) -> str:
    """Return only a fixed diagnostic label; never retain service text or URLs."""
    from tools.youtube.verify_podcast_candidates import MEMBER_RE

    # Signed URL paths/query strings are not diagnostic evidence.
    text = re.sub(r"https?://\S+", "", stderr.lower())
    if any(message in text for message in (
        "not a bot", "sign in to confirm", "sign in to verify", "login required",
        "please sign in", "sign in required",
    )):
        return "bot_or_sign_in_challenge"
    if re.search(r"\b429\b", text) and ("http" in text or "too many requests" in text):
        return "http_429_rate_limit"
    if re.search(r"\b403\b", text) and ("http" in text or "forbidden" in text):
        return "http_403_forbidden"
    if MEMBER_RE.search(text):
        return "member_only"
    if any(message in text for message in (
        "javascript runtime", "challenge solver", "challenge solving failed", "[jsc]",
        "signature extraction failed", "nsig extraction failed", "signature solving failed",
    )):
        return "javascript_runtime_or_challenge_solver"
    if any(message in text for message in (
        "requested format is not available", "no video formats found", "no formats found",
        "only images are available", "no suitable formats",
    )):
        return "format_unavailable"
    if any(message in text for message in (
        "timed out", "timeout", "connection refused", "connection reset", "connection aborted",
        "temporary failure in name resolution", "name or service not known", "nodename nor servname",
        "network is unreachable", "remote end closed connection", "certificate verify failed",
    )):
        return "network_error"
    return "unclassified_extraction_error"


def probe(video_id: str) -> dict:
    result = subprocess.run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-warnings",
        "--socket-timeout", "30", "--retries", "3", "--extractor-retries", "3",
        "--skip-download", "--dump-single-json", f"https://www.youtube.com/watch?v={video_id}"],
        cwd=ROOT, text=True, capture_output=True, timeout=600)
    if result.returncode:
        from tools.youtube.verify_podcast_candidates import ERROR_RE, MEMBER_RE
        for line in result.stderr.splitlines():
            match = ERROR_RE.match(line)
            if match and match.group(1) == video_id and MEMBER_RE.search(match.group(2)):
                return {"id": video_id, "channel_id": CHANNEL_ID, "availability": "subscriber_only"}
        label = classify_youtube_error(result.stderr)
        raise CloudBlocked(f"YouTube probe failed: {video_id}; diagnostic={label}")
    return json.loads(result.stdout)


def download(info: dict) -> None:
    folder = folder_for(ROOT, info)
    folder.mkdir(parents=True, exist_ok=True)
    # Redownload unfinished candidates on every fresh runner; a past download is not publication.
    result = subprocess.run([
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-warnings", "--no-progress",
        "--retries", "3", "--fragment-retries", "3", "-f", "bestaudio", "--extract-audio", "--audio-format", "m4a",
        "--output", str(folder / "source.%(ext)s"), f"https://www.youtube.com/watch?v={info['id']}",
    ], cwd=ROOT, text=True, capture_output=True, timeout=5400)
    if result.returncode:
        label = classify_youtube_error(result.stderr)
        raise CloudBlocked(f"Cloud audio download failed: {info['id']}; diagnostic={label}")
    audio = folder / "source.m4a"
    if not audio.is_file() or not audio.stat().st_size:
        raise CloudBlocked(f"No usable audio after download: {info['id']}")
    atomic_write_json(folder / "source.info.json", minimal_info(info))
    (folder / "source.description").write_text(str(info.get("description") or ""), encoding="utf-8")


def stream_runner(command: list[str], log) -> list[str]:
    # Capture raw stderr privately in memory: service errors can contain signed URLs.
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=5400)
    if result.returncode:
        raise CloudBlocked(f"Execution/check failed: {Path(command[1]).name}; reconcile pending targets before retry")
    return result.stdout.splitlines()


def json_runner(command: list[str], log) -> dict:
    return json.loads("\n".join(stream_runner(command, log)))


def standing_policy(policy: dict) -> bool:
    if policy.get("enabled") is not True:
        return False
    expected = {"schema_version": 1, "show_id": SHOW_ID, "channel_id": CHANNEL_ID,
                "max_items": 3, "scope": "new_public_normal_only", "local_writer_disabled": True}
    if any(policy.get(k) != v for k, v in expected.items()) or not policy.get("approved_by") or not policy.get("approved_at"):
        raise CloudBlocked("Enabled publication policy lacks an explicit complete standing grant or local cutover")
    return True


def validate_live_candidates(plan: dict, ops: Path) -> None:
    # Fresh identity/privacy check even when applying a still-valid frozen artifact.
    excluded_path = ops / "excluded-videos.json"
    excluded = json.loads(excluded_path.read_text()).get("videos", {}) if excluded_path.exists() else {}
    for action in plan.get("publish_actions", []):
        video_id = action["local"]["video_id"]
        if video_id in excluded:
            raise CloudBlocked(f"Candidate has an explicit content exclusion: {video_id}")
        if verify_probe(video_id, probe(video_id)) != "public":
            raise CloudBlocked(f"Candidate is no longer public normal content: {video_id}")


def pending_targets(plan: dict) -> dict:
    targets = {}
    projected = {row["video_id"]: row for row in plan["projected_feed"]}
    for action in plan["publish_actions"]:
        local = action["local"]
        row = projected[local["video_id"]]
        targets[local["video_id"]] = {"title": row["target_title"], "number": row["target_number"],
                                      "description_sha256": local["description_sha256"],
                                      "published_at": local["published_at"]}
    return {"started_at": utc_now(), "plan_hash": plan["plan_hash"], "targets": targets}


def recover_pending(state: dict, episodes: list[dict]) -> None:
    pending = state.get("pending_execution")
    if not pending:
        return
    mapping = {}
    for episode in episodes:
        video_id = episode_video_id(episode.get("attributes", {}))
        if video_id not in pending["targets"]:
            continue
        if video_id in mapping:
            raise CloudBlocked("Duplicate remote association during recovery")
        mapping[video_id] = episode
    published = []
    for video_id, target in pending["targets"].items():
        episode = mapping.get(video_id)
        if not episode or episode.get("attributes", {}).get("status") != "published":
            continue
        attrs = episode["attributes"]
        if (attrs.get("title") != target["title"] or attrs.get("number") != target["number"]
                or sha256_text(str(attrs.get("description") or "").strip()) != target["description_sha256"]
                or str(attrs.get("published_at") or "")[:10] != target["published_at"][:10]):
            raise CloudBlocked(f"Pending publication differs from reviewed payload: {video_id}")
        stream_runner([sys.executable, "tools/check/check_upload_quality.py", "--video-id", video_id], None)
        published.append(video_id)
    if published:
        reorder = json_runner([sys.executable, "tools/upload/reorder_episodes_by_date.py", "--json"], None)
        if reorder.get("blocked_reasons") or int(reorder.get("action_count", 0)):
            raise CloudBlocked("Recovered publication needs a separate historical-number review; no repair was applied")
    state["last_recovery"] = {"checked_at": utc_now(), "published_verified": published,
                               "remaining": sorted(set(pending["targets"]) - set(published)),
                               "plan_hash": pending["plan_hash"]}
    if not state["last_recovery"]["remaining"]:
        state["pending_execution"] = None


def build_cloud_plan(state: dict, ops: Path, episodes: list[dict], receipt: dict) -> dict | None:
    from tools.youtube.fetch_public_videos import fetch
    from tools.check.build_podcast_sync_plan import build_plan
    videos = fetch(CHANNEL_ID)
    candidates = select_candidates(videos, episodes)
    unresolved = set((state.get("last_recovery") or {}).get("remaining", [])) if state.get("pending_execution") else set()
    if unresolved - {row["video_id"] for row in candidates}:
        raise CloudBlocked("Unfinished publication targets fell behind the latest published baseline; explicit recovery review required")
    exclusions_path = ops / "excluded-videos.json"
    exclusions = json.loads(exclusions_path.read_text()).get("videos", {}) if exclusions_path.exists() else {}
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", key) or not value.get("basis") for key, value in exclusions.items()):
        raise CloudBlocked("Invalid explicit content exclusions")
    state["last_discovery_at"] = utc_now()
    state["last_discovered_ids"] = [row["video_id"] for row in candidates]
    atomic_write_json(ROOT / SNAPSHOTS[0], {"schema_version": 1, "generated_at": utc_now(),
        "source": "anonymous_youtube_channel_videos", "channel_id": CHANNEL_ID, "videos": videos, "video_count": len(videos)})
    catalog = load_catalog(ops / "podcast_series.json")
    eligible, pending, verified = [], [], []
    for row in candidates:
        video_id = row["video_id"]
        if video_id in exclusions:
            if video_id in unresolved:
                raise CloudBlocked("Pending publication target was excluded; explicit recovery review required")
            verified.append({"video_id": video_id, "status": "excluded_by_content_decision", "verified_at": utc_now()})
            continue
        info = probe(video_id)
        status = verify_probe(video_id, info)
        verified.append({"video_id": video_id, "status": status, "verified_at": utc_now()})
        if status != "public":
            if video_id in unresolved:
                raise CloudBlocked(f"Unfinished publication target is now excluded: {video_id}; explicit recovery review required")
            continue
        state["records"][video_id] = minimal_info(info)
        if video_id not in catalog:
            pending.append({"video_id": video_id, "title": info.get("title"), "upload_date": info.get("upload_date"),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "description": str(info.get("description") or "")[:10000],
                            "action": "classify_dialogue_or_solo_with_evidence"})
        eligible.append(info)
    receipt["candidate_checks"] = verified
    receipt["dot_actions"] = pending
    # Preserve classification tasks durably even when audio/artifacts have expired.
    state["dot_actions"] = pending
    if len(eligible) > MAX_ITEMS:
        raise CloudBlocked("More than 3 eligible new videos; review backlog and split batches explicitly")
    if pending:
        receipt["status"] = "needs_classification"
        return None
    restore_metadata(ROOT, state)
    for info in eligible:
        download(info)
        sidecar = ops / "podcast_show_notes" / f"{info['id']}.txt"
        if sidecar.is_file():
            target = ROOT / "podcast_show_notes" / sidecar.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(sidecar, target)
    # Stamp verification after downloads; actual proof time is retained per candidate.
    atomic_write_json(ROOT / SNAPSHOTS[1], {"schema_version": 1, "generated_at": utc_now(),
        "source": "bounded_anonymous_yt_dlp_probe", "channel_id": CHANNEL_ID,
        "candidate_count": len(verified), "public_count": len(eligible), "candidates": verified})
    plan = build_plan(MAX_AGE_HOURS, manifest_out_dir=ROOT / "logs/cloud/manifest")
    atomic_write_json(ROOT / PLAN_PATH, plan)
    return plan


def verify_pending_coverage(state: dict, plan: dict) -> None:
    pending = state.get("pending_execution")
    if not pending:
        return
    remaining = set((state.get("last_recovery") or {}).get("remaining", pending["targets"]))
    planned = {item["local"]["video_id"] for item in plan.get("publish_actions", [])}
    if remaining - planned:
        raise CloudBlocked("New plan omits unfinished publication targets; explicit recovery or cancellation review required")


def run(args) -> int:
    ops = args.ops.resolve()
    state_path = ops / "runtime/state.json"
    state = validate_state(json.loads(state_path.read_text(encoding="utf-8")))
    receipt = {"started_at": utc_now(), "mode": args.mode, "status": "failed", "show_id": SHOW_ID,
               "run_id": os.environ.get("GITHUB_RUN_ID"), "destination": "https://pod.lizheng.ai",
               "audience": "public podcast RSS subscribers and website visitors"}
    persist = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        if persist:
            guard_ops(ops)
        key, show_id = require_transistor_config()
        if show_id != SHOW_ID:
            raise CloudBlocked("Unexpected Transistor show")
        code_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if (ops / "code-version.txt").read_text().strip() != code_sha:
            raise CloudBlocked("Source checkout differs from pinned operational version")
        shutil.copyfile(ops / "podcast_series.json", ROOT / "podcast_series.json")
        restore_metadata(ROOT, state)
        episodes = TransistorClient(key).list_episodes(SHOW_ID)
        # This also validates all remote identities before any pending-state recovery.
        for episode in episodes:
            if episode.get("attributes", {}).get("status") == "published":
                video_id = episode_video_id(episode["attributes"])
                if not video_id or video_id not in state["records"]:
                    raise CloudBlocked("Published episode lacks canonical historical date metadata")
        recover_pending(state, episodes)
        if args.mode == "publish":
            policy = json.loads((ops / "publication-policy.json").read_text())
            if policy.get("local_writer_disabled") is not True:
                raise CloudBlocked("Local writer must be disabled before cloud publication")
            plan = import_bundle(args.bundle, ROOT, code_sha=code_sha, catalog=ops / "podcast_series.json",
                                 plan_hash=args.plan_hash, approval_hash=args.approval_hash)
        else:
            plan = build_cloud_plan(state, ops, episodes, receipt)
        if plan is not None:
            decision = evaluate_auto_publish(plan, ROOT, max_items=MAX_ITEMS)
            receipt["decision"] = decision
            receipt["plan_hash"] = plan["plan_hash"]
            receipt["approval_hash"] = plan["publish_approval_hash"]
            if args.mode != "publish":
                export_bundle(ROOT, ROOT / "logs/cloud/bundle", code_sha=code_sha, catalog=ops / "podcast_series.json")
            auto = args.mode == "auto" and standing_policy(json.loads((ops / "publication-policy.json").read_text()))
            requested = args.mode == "publish" or auto
            if decision["blockers"]:
                receipt["status"] = "blocked"
                if requested:
                    raise CloudBlocked("Publication eligibility checks failed; see receipt decision blockers")
            elif not decision["has_candidates"]:
                receipt["status"] = "no_new_episodes"
            elif not requested:
                receipt["status"] = "awaiting_approval"
            else:
                validate_live_candidates(plan, ops)
                verify_pending_coverage(state, plan)
                state["pending_execution"] = pending_targets(plan)
                checkpoint(ops, state)  # A failed durable write MUST prevent the first remote mutation.
                from tools.automation.sync_podcast import EventLog, execute_auto_publish
                result = execute_auto_publish(plan_path=ROOT / PLAN_PATH, plan=plan, log=EventLog(),
                    max_snapshot_age_hours=MAX_AGE_HOURS, stream_runner=stream_runner, json_runner=json_runner)
                receipt["result"] = result
                state["pending_execution"] = None
                state["last_verified_publish_at"] = utc_now()
                receipt["status"] = "published_verified"
        state["last_successful_check_at"] = utc_now()
        return 0
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["error_type"] = type(exc).__name__
        receipt["error"] = str(exc) if isinstance(exc, CloudBlocked) else "Operation failed; raw service response omitted to protect credentials and signed URLs"
        return 1
    finally:
        receipt["finished_at"] = utc_now()
        state["last_checked_at"] = receipt["finished_at"]
        state["latest_receipt"] = receipt
        atomic_write_json(state_path, state)
        atomic_write_json(ROOT / "logs/cloud/receipt.json", receipt)
        if persist:
            try:
                checkpoint(ops, state)
            except Exception:
                print("State checkpoint failed; inspect private receipt artifact and reconcile pending remote targets before retry", file=sys.stderr)
                # Even a successful publication must report a failed workflow if state persistence failed.
                raise CloudBlocked("Could not save durable operational state") from None
        print(json.dumps({"status": receipt["status"], "receipt": "logs/cloud/receipt.json"}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ops", type=Path, required=True)
    parser.add_argument("--mode", choices=("plan", "publish", "auto"), default="plan")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--approval-hash", default="")
    parser.add_argument("--plan-hash", default="")
    parser.add_argument("--bootstrap-from", type=Path)
    args = parser.parse_args()
    if args.bootstrap_from:
        print(json.dumps(bootstrap(args.bootstrap_from, args.ops), ensure_ascii=False))
        return 0
    if args.mode == "publish" and not args.bundle:
        parser.error("--bundle is required for reviewed publication")
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CloudBlocked as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
