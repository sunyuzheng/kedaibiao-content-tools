#!/usr/bin/env python3
"""Unattended, read-mostly YouTube -> Transistor reconciliation.

The scheduled job downloads new local media, refreshes one canonical remote
snapshot, and writes an immutable approval plan. It never publishes or edits
Transistor on its own.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import signal
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.automation.email_notification import send_report  # noqa: E402
from tools.podcast.core import load_env  # noqa: E402


LOG_DIR = PROJECT_ROOT / "logs" / "podcast_sync"
LOCK_FILE = LOG_DIR / "podcast_sync.lock"
HEARTBEAT = LOG_DIR / "latest-run.json"
CANDIDATE_VERIFICATION_SNAPSHOT = (
    PROJECT_ROOT / "tools" / "youtube" / "podcast_candidate_verification.json"
)
PYTHON = sys.executable
YOUTUBE_PYTHON = PROJECT_ROOT / "envs" / "youtube_env" / "bin" / "python"
EXPECTED_YOUTUBE_CHANNEL_ID = "UC_5lJHgnMP_lb_VpIiXV0hQ"
ACTIVE_CHILD: subprocess.Popen[str] | None = None


class EventLog:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"sync-{stamp}.jsonl"

    def emit(self, event: str, **data: Any) -> None:
        item = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        line = json.dumps(item, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        print(line, flush=True)


def notify(title: str, message: str) -> None:
    """Best-effort local notification; never fail the pipeline."""
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def notify_email(report: dict[str, Any]) -> dict[str, Any]:
    """Keep notification failures from changing the reconciliation result."""
    try:
        return send_report(report)
    except Exception as exc:
        return {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_stream(command: list[str], log: EventLog) -> None:
    global ACTIVE_CHILD
    log.emit("command_started", command=command)
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=child_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    ACTIVE_CHILD = process
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if line:
            log.emit("command_output", command=command[0], line=line)
    returncode = process.wait()
    ACTIVE_CHILD = None
    log.emit("command_completed", command=command, returncode=returncode)
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


def handle_shutdown(signum: int, _frame: Any) -> None:
    """Forward launchd termination to the whole active child process group."""
    child = ACTIVE_CHILD
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)


def run_json(command: list[str], log: EventLog) -> dict[str, Any]:
    log.emit("command_started", command=command)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=child_env(),
        text=True,
        capture_output=True,
    )
    if result.stderr.strip():
        for line in result.stderr.splitlines():
            log.emit("command_output", command=command[0], line=line)
    log.emit("command_completed", command=command, returncode=result.returncode)
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )
    return json.loads(result.stdout)


def rotate_logs(now: datetime) -> None:
    """Compress old run logs and retain 180 days of diagnostics."""
    compress_before = now - timedelta(days=7)
    delete_before = now - timedelta(days=180)
    for path in LOG_DIR.glob("sync-*.jsonl"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified >= compress_before:
            continue
        gzip_path = path.with_suffix(path.suffix + ".gz")
        with path.open("rb") as source, gzip_path.open("wb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as archive:
                shutil.copyfileobj(source, archive)
        path.unlink()
    for path in LOG_DIR.glob("sync-*.jsonl.gz"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < delete_before:
            path.unlink()


def file_age_hours(path: Path, now: datetime) -> float | None:
    if not path.exists():
        return None
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0.0, (now - modified).total_seconds() / 3600)


def candidate_evidence_matches_plan(plan_path: Path) -> bool:
    if not CANDIDATE_VERIFICATION_SNAPSHOT.exists():
        return False
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    evidence = json.loads(
        CANDIDATE_VERIFICATION_SNAPSHOT.read_text(encoding="utf-8")
    )
    planned = {
        item["local"]["video_id"]
        for item in plan.get("candidate_publish_actions", [])
    }
    observed = {
        item["video_id"]
        for item in evidence.get("candidates", [])
        if item.get("video_id")
    }
    return planned == observed


def main() -> int:
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-youtube-refresh", action="store_true")
    parser.add_argument("--no-notification", action="store_true")
    parser.add_argument("--force-candidate-verification", action="store_true")
    parser.add_argument("--reuse-candidate-verification-hours", type=float, default=6)
    parser.add_argument("--max-youtube-snapshot-age-hours", type=float, default=72)
    args = parser.parse_args()
    load_env()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = EventLog()
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.emit("run_skipped", reason="another_sync_is_running")
            return 0

        started_at = datetime.now(timezone.utc)
        try:
            log.emit("run_started", mode="plan_only")
            run_stream(
                [
                    PYTHON,
                    "tools/automation/update_yt_dlp.py",
                ],
                log,
            )
            run_stream(
                [
                    PYTHON,
                    "tools/youtube/fetch_public_videos.py",
                    "--channel-id",
                    EXPECTED_YOUTUBE_CHANNEL_ID,
                ],
                log,
            )
            if not args.skip_youtube_refresh:
                try:
                    if not YOUTUBE_PYTHON.exists():
                        raise FileNotFoundError(f"Missing YouTube environment: {YOUTUBE_PYTHON}")
                    run_stream(
                        [
                            str(YOUTUBE_PYTHON),
                            "tools/youtube/fetch_all_videos.py",
                            "--non-interactive",
                            "--expected-channel-id",
                            EXPECTED_YOUTUBE_CHANNEL_ID,
                        ],
                        log,
                    )
                except Exception as exc:
                    # Continue to a fail-closed plan. The stale snapshot gate will
                    # prevent publication and the notification will request action.
                    log.emit(
                        "youtube_refresh_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
            if not args.skip_download:
                run_stream(
                    [
                        "./tools/download/download_channel.sh",
                        "--skip-listing-refresh",
                    ],
                    log,
                )
            preliminary = run_json(
                [
                    PYTHON,
                    "tools/check/build_podcast_sync_plan.py",
                    "--json",
                    "--max-youtube-snapshot-age-hours",
                    str(args.max_youtube_snapshot_age_hours),
                ],
                log,
            )
            evidence_age = file_age_hours(
                CANDIDATE_VERIFICATION_SNAPSHOT,
                datetime.now(timezone.utc),
            )
            preliminary_path = Path(preliminary["plan_path"])
            if (
                not args.force_candidate_verification
                and evidence_age is not None
                and evidence_age <= args.reuse_candidate_verification_hours
                and candidate_evidence_matches_plan(preliminary_path)
            ):
                log.emit(
                    "candidate_verification_skipped",
                    reason="recent_matching_evidence",
                    evidence_age_hours=round(evidence_age, 3),
                )
            else:
                run_stream(
                    [
                        PYTHON,
                        "tools/youtube/verify_podcast_candidates.py",
                        "--plan",
                        str(preliminary_path),
                    ],
                    log,
                )
            summary = run_json(
                [
                    PYTHON,
                    "tools/check/build_podcast_sync_plan.py",
                    "--json",
                    "--max-youtube-snapshot-age-hours",
                    str(args.max_youtube_snapshot_age_hours),
                ],
                log,
            )
            status = "attention" if (
                summary["publish_count"]
                or summary["transcript_count"]
                or summary["blocked_count"]
                or not summary["youtube_snapshot_fresh"]
            ) else "healthy"
            heartbeat = {
                "started_at": started_at.isoformat(timespec="seconds"),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": status,
                **summary,
            }
            HEARTBEAT.write_text(
                json.dumps(heartbeat, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            log.emit("run_completed", **heartbeat)
            if not args.no_notification:
                if status == "attention":
                    notify(
                        "课代表播客同步待确认",
                        (
                            f"发布 {summary['publish_count']}，字幕 "
                            f"{summary['transcript_count']}，阻止 "
                            f"{summary['blocked_count']}。已生成审批计划。"
                        ),
                    )
                log.emit("email_notification", **notify_email(heartbeat))
            rotate_logs(datetime.now(timezone.utc))
            return 0
        except Exception as exc:
            failure = {
                "started_at": started_at.isoformat(timespec="seconds"),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "log_path": str(log.path),
            }
            HEARTBEAT.write_text(
                json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            log.emit("run_failed", **failure)
            if not args.no_notification:
                notify("课代表播客同步失败", f"{type(exc).__name__}: {exc}")
                log.emit("email_notification", **notify_email(failure))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
