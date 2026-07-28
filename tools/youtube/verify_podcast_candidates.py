#!/usr/bin/env python3
"""Verify only current podcast publication candidates with yt-dlp."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = PROJECT_ROOT / "logs" / "podcast_sync" / "plans" / "latest.json"
DEFAULT_OUT = PROJECT_ROOT / "tools" / "youtube" / "podcast_candidate_verification.json"
LISTING = PROJECT_ROOT / "tools" / "youtube" / "public_videos_snapshot.json"
YTDLP = PROJECT_ROOT / ".venv-podcast" / "bin" / "yt-dlp"
CHANNEL_ID = "UC_5lJHgnMP_lb_VpIiXV0hQ"
ERROR_RE = re.compile(r"^ERROR: \[youtube\] ([^:]+): (.*)$")
MEMBER_RE = re.compile(
    r"members?.only|join this channel|subscriber.only|"
    r"available to this channel.s members",
    re.IGNORECASE,
)


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--verified-ids-file",
        type=Path,
        help="Import IDs from a read-only probe instead of networking",
    )
    parser.add_argument("--expected-channel-id", default=CHANNEL_ID)
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    candidates = sorted({
        item["local"]["video_id"]
        for item in plan.get("candidate_publish_actions", [])
    })
    listing = json.loads(LISTING.read_text(encoding="utf-8"))
    if listing.get("channel_id") != args.expected_channel_id:
        raise RuntimeError("YouTube listing channel id mismatch")
    listing_ids = {
        item.get("video_id")
        for item in listing.get("videos", [])
        if item.get("video_id")
    }
    unknown = sorted(set(candidates) - listing_ids)
    if unknown:
        raise RuntimeError(f"Candidates absent from current listing: {unknown}")

    verified_ids: set[str] = set()
    errors: dict[str, str] = {}
    source = "imported_yt_dlp_probe"
    if args.verified_ids_file:
        verified_ids = {
            line.strip()
            for line in args.verified_ids_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        } & set(candidates)
    elif candidates:
        source = "bounded_yt_dlp_candidate_probe"
        if not YTDLP.exists():
            raise RuntimeError(f"Missing project yt-dlp: {YTDLP}")
        with tempfile.TemporaryDirectory(prefix="kedaibiao-candidate-probe-") as directory:
            ids_path = Path(directory) / "verified.txt"
            command = [
                str(YTDLP),
                "--skip-download",
                "--ignore-errors",
                "--socket-timeout", "30",
                "--retries", "3",
                "--extractor-retries", "3",
                "--sleep-requests", "2",
                "--print-to-file", "after_filter:%(id)s", str(ids_path),
                *[f"https://www.youtube.com/watch?v={video_id}" for video_id in candidates],
            ]
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=20 * 60,
            )
            if ids_path.exists():
                verified_ids = {
                    line.strip()
                    for line in ids_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
            for line in result.stderr.splitlines():
                match = ERROR_RE.match(line)
                if not match:
                    continue
                video_id, message = match.groups()
                errors[video_id] = (
                    "member_only" if MEMBER_RE.search(message) else message[:500]
                )
            if result.returncode and not errors:
                raise RuntimeError(
                    f"yt-dlp candidate probe failed ({result.returncode}): "
                    f"{result.stderr[-1000:]}"
                )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for video_id in candidates:
        if video_id in verified_ids:
            status, detail = "public", None
        elif errors.get(video_id) == "member_only":
            status, detail = "member_only", "member_only"
        else:
            status, detail = "unverified", (
                errors.get(video_id) or "not_observed_in_probe"
            )
        rows.append({
            "video_id": video_id,
            "status": status,
            "verified_at": now if status == "public" else None,
            "detail": detail,
        })
    payload = {
        "schema_version": 1,
        "generated_at": now,
        "source": source,
        "channel_id": args.expected_channel_id,
        "source_plan_path": str(args.plan),
        "source_plan_hash": plan.get("plan_hash"),
        "candidate_count": len(candidates),
        "public_count": sum(row["status"] == "public" for row in rows),
        "member_only_count": sum(row["status"] == "member_only" for row in rows),
        "unverified_count": sum(row["status"] == "unverified" for row in rows),
        "candidates": rows,
    }
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "path": str(args.out),
        "candidate_count": payload["candidate_count"],
        "public_count": payload["public_count"],
        "member_only_count": payload["member_only_count"],
        "unverified_count": payload["unverified_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
