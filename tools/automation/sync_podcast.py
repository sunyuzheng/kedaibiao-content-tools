#!/usr/bin/env python3
"""Unattended YouTube -> Transistor reconciliation and strict auto-publish.

The scheduled job downloads new local media, refreshes one canonical remote
snapshot, and writes an immutable plan. A small, strictly verified incremental
batch may be published through the existing fail-closed executor. Completion is
reported only after remote readback, per-episode quality checks, reorder audit,
and a fresh reconciliation plan all succeed.
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
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.automation.email_notification import send_report  # noqa: E402
from tools.automation.podcast_auto_publish import (  # noqa: E402
    DEFAULT_MAX_AUTO_PUBLISH_ITEMS,
    evaluate_auto_publish,
)
from tools.automation.podcast_sync_report import (  # noqa: E402
    PUBLIC_FEED_URL,
    build_completion_report,
    build_execution_failure_report,
    build_reconciliation_report,
    load_notification_state,
)
from tools.podcast.core import atomic_write_json, load_env  # noqa: E402


LOG_DIR = PROJECT_ROOT / "logs" / "podcast_sync"
LOCK_FILE = LOG_DIR / "podcast_sync.lock"
HEARTBEAT = LOG_DIR / "latest-run.json"
NOTIFICATION_STATE = LOG_DIR / "notification-state.json"
EXECUTION_LEDGER_DIR = LOG_DIR / "ledgers"
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


def run_stream(command: list[str], log: EventLog) -> list[str]:
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
    output_lines: list[str] = []
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if line:
            output_lines.append(line)
            log.emit("command_output", command=command[0], line=line)
    returncode = process.wait()
    ACTIVE_CHILD = None
    log.emit("command_completed", command=command, returncode=returncode)
    if returncode:
        raise subprocess.CalledProcessError(
            returncode,
            command,
            output="\n".join(output_lines),
        )
    return output_lines


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


class AutoPublishExecutionError(RuntimeError):
    """Carry stage and confirmed partial publications to owner reporting."""

    def __init__(
        self,
        stage: str,
        error: str,
        *,
        partial_publications: list[dict[str, Any]] | None = None,
        ledger_path: str | None = None,
    ) -> None:
        super().__init__(error)
        self.stage = stage
        self.partial_publications = partial_publications or []
        self.ledger_path = ledger_path


def execution_mode(auto_decision: dict[str, Any], *, dry_run: bool) -> str:
    """Keep the dry-run boundary explicit and unit-testable."""
    if dry_run:
        return "dry_run"
    if auto_decision.get("eligible"):
        return "execute"
    return "none"


def notification_delivery_enabled(*, no_notification: bool, dry_run: bool) -> bool:
    """Keep dry-runs silent on success and failure paths alike."""
    return not no_notification and not dry_run


def _executor_receipts(lines: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    published: list[dict[str, Any]] = []
    ledger_path: str | None = None
    for line in lines:
        if line.startswith("Ledger: "):
            ledger_path = line.split(": ", 1)[1].strip()
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") == "episode_published":
            published.append(
                {
                    "video_id": payload.get("video_id"),
                    "episode_id": payload.get("episode_id"),
                    "published_at": payload.get("published_at"),
                    "transistor_result": "published + exact API readback verified",
                    "transcript_verification": payload.get("transcript_verification"),
                }
            )
    return published, ledger_path


def _latest_matching_ledger(plan_hash_value: str | None) -> str | None:
    """Recover the executor ledger path when a failed child never prints it."""
    if not plan_hash_value or not EXECUTION_LEDGER_DIR.exists():
        return None
    paths = sorted(
        EXECUTION_LEDGER_DIR.glob("publish-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths[:10]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = handle.readline()
            payload = json.loads(first)
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("plan_hash") == plan_hash_value:
            return str(path)
    return None


def check_public_feed(
    video_ids: list[str],
    *,
    expected_titles: dict[str, str] | None = None,
    feed_url: str = PUBLIC_FEED_URL,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Return an honest RSS propagation receipt; lag is not a publish failure.

    Transistor does not expose ``episode.video_url`` as a dedicated RSS field.
    A valid description may therefore omit the YouTube id even when the episode
    is already present. Match the immutable target title as a second stable
    signal so completion receipts do not confuse that omission with feed lag.
    """
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        response = (session or requests.Session()).get(
            feed_url,
            timeout=(10, 30),
            headers={"User-Agent": "kedaibiao-podcast-post-publish-check/1.0"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return {
            "status": "unavailable",
            "checked_at": checked_at,
            "detail": f"公开 RSS 检查暂时不可用：{type(exc).__name__}",
        }
    body = response.text
    rss_titles: set[str] = set()
    try:
        root = ET.fromstring(body)
        rss_titles = {
            str(item.findtext("title") or "").strip()
            for item in root.findall(".//item")
        }
    except ET.ParseError:
        # Keep the prior conservative id check if the upstream body is not
        # parseable XML. A malformed feed must never be reported as observed
        # from a title substring alone.
        rss_titles = set()
    title_aware = expected_titles is not None
    titles = expected_titles or {}
    observed: list[str] = []
    for video_id in video_ids:
        if title_aware:
            expected_title = str(titles.get(video_id) or "").strip()
            if expected_title and expected_title in rss_titles:
                observed.append(video_id)
        elif video_id in body:
            observed.append(video_id)
    observed.sort()
    missing = sorted(set(video_ids) - set(observed))
    if missing:
        return {
            "status": "propagation_pending",
            "checked_at": checked_at,
            "observed_video_ids": observed,
            "missing_video_ids": missing,
            "detail": "Transistor 已发布，但本次抓取尚未在公开 RSS 看到全部目标；可能仍在传播。",
        }
    return {
        "status": "observed",
        "checked_at": checked_at,
        "observed_video_ids": observed,
        "missing_video_ids": [],
        "detail": "公开 RSS 已观察到全部目标节目。",
    }


def execute_auto_publish(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    log: EventLog,
    max_snapshot_age_hours: float,
    stream_runner: Any = run_stream,
    json_runner: Any = run_json,
    feed_checker: Any = None,
) -> dict[str, Any]:
    """Execute one eligible plan and require every critical post-check."""
    expected_ids = {
        str(item.get("local", {}).get("video_id") or "")
        for item in plan.get("publish_actions", [])
    }
    executor_command = [
        PYTHON,
        "tools/upload/apply_podcast_sync_plan.py",
        "--plan",
        str(plan_path),
        "--prepare-and-publish",
        # The executor's schema-compatible flag carries the immutable publish
        # scope integrity hash; no human approval is inferred by this workflow.
        "--approval-hash",
        str(plan.get("publish_approval_hash") or ""),
    ]
    executor_lines: list[str] = []
    try:
        executor_lines = stream_runner(executor_command, log)
    except Exception as exc:
        output = exc.output if isinstance(exc, subprocess.CalledProcessError) else ""
        lines = str(output or "").splitlines()
        partial, ledger_path = _executor_receipts(lines)
        ledger_path = ledger_path or _latest_matching_ledger(plan.get("plan_hash"))
        raise AutoPublishExecutionError(
            "publish_executor",
            str(exc),
            partial_publications=partial,
            ledger_path=ledger_path,
        ) from exc
    published, ledger_path = _executor_receipts(executor_lines)
    ledger_path = ledger_path or _latest_matching_ledger(plan.get("plan_hash"))
    observed_ids = {str(item.get("video_id") or "") for item in published}
    if observed_ids != expected_ids:
        raise AutoPublishExecutionError(
            "publish_receipt",
            f"executor receipts differ: expected={sorted(expected_ids)} observed={sorted(observed_ids)}",
            partial_publications=published,
            ledger_path=ledger_path,
        )

    try:
        for video_id in sorted(expected_ids):
            stream_runner(
                [
                    PYTHON,
                    "tools/check/check_upload_quality.py",
                    "--video-id",
                    video_id,
                ],
                log,
            )
    except Exception as exc:
        raise AutoPublishExecutionError(
            "per_episode_quality",
            str(exc),
            partial_publications=published,
            ledger_path=ledger_path,
        ) from exc

    try:
        reorder = json_runner(
            [
                PYTHON,
                "tools/upload/reorder_episodes_by_date.py",
                "--json",
            ],
            log,
        )
        if reorder.get("blocked_reasons") or int(reorder.get("action_count", 0)):
            raise RuntimeError(
                "post-publish reorder is not clean: "
                f"blocked={reorder.get('blocked_reasons')} actions={reorder.get('action_count')}"
            )
    except Exception as exc:
        raise AutoPublishExecutionError(
            "reorder_check",
            str(exc),
            partial_publications=published,
            ledger_path=ledger_path,
        ) from exc

    try:
        rebuilt_summary = json_runner(
            [
                PYTHON,
                "tools/check/build_podcast_sync_plan.py",
                "--json",
                "--max-youtube-snapshot-age-hours",
                str(max_snapshot_age_hours),
            ],
            log,
        )
        rebuilt_path = Path(str(rebuilt_summary["plan_path"]))
        rebuilt = json.loads(rebuilt_path.read_text(encoding="utf-8"))
        rebuilt_candidate_ids = {
            str(item.get("local", {}).get("video_id") or "")
            for key in ("publish_actions", "candidate_publish_actions")
            for item in rebuilt.get(key, [])
        }
        still_pending = sorted(expected_ids & rebuilt_candidate_ids)
        if still_pending:
            raise RuntimeError(
                f"published targets are still pending in rebuilt plan: {still_pending}"
            )
    except Exception as exc:
        raise AutoPublishExecutionError(
            "rebuild_plan",
            str(exc),
            partial_publications=published,
            ledger_path=ledger_path,
        ) from exc

    try:
        use_default_feed_checker = feed_checker is None
        if use_default_feed_checker:
            feed_checker = check_public_feed
        if use_default_feed_checker or feed_checker is check_public_feed:
            expected_titles = {
                str(row.get("video_id") or ""): str(row.get("target_title") or "")
                for row in plan.get("projected_feed", [])
                if str(row.get("video_id") or "") in expected_ids
            }
            rss = feed_checker(
                sorted(expected_ids),
                expected_titles=expected_titles,
            )
        else:
            rss = feed_checker(sorted(expected_ids))
    except Exception as exc:
        rss = {
            "status": "unavailable",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "detail": f"公开 RSS 检查暂时不可用：{type(exc).__name__}",
        }
    rss_detail = str(rss.get("detail") or rss.get("status") or "unknown")
    downstream = (
        "公开 RSS 已观察到目标；小宇宙等客户端仍按各自抓取/缓存节奏刷新，未直接验证客户端。"
        if rss.get("status") == "observed"
        else "Transistor 已发布；RSS/小宇宙传播仍可能延迟，本次未声称客户端已刷新。"
    )
    return {
        "published_items": published,
        "ledger_path": ledger_path,
        "post_publish_checks": {
            "quality": f"通过（{len(expected_ids)} 期逐集 API GET）",
            "reorder": "通过（0 个待重排动作；未改动历史 episode）",
            "rebuild": f"通过（{len(expected_ids)} 个目标已从待发布候选消失）",
        },
        "platform_results": {
            "transistor": (
                f"{len(published)} 期 published + exact readback verified；"
                + ", ".join(
                    f"{item.get('video_id')}→episode {item.get('episode_id')}"
                    for item in published
                )
            ),
            "rss": rss_detail,
            "downstream": downstream,
        },
        "rss_receipt": rss,
        "rebuilt_plan_path": str(rebuilt_path),
    }


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and evaluate the exact plan, but never invoke the publish executor or send email.",
    )
    parser.add_argument(
        "--max-auto-publish-items",
        type=int,
        default=DEFAULT_MAX_AUTO_PUBLISH_ITEMS,
        help="Optionally lower the hard daily incremental batch limit of 3.",
    )
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
            log.emit(
                "run_started",
                mode="dry_run" if args.dry_run else "strict_auto_publish",
                max_auto_publish_items=args.max_auto_publish_items,
            )
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
            planned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            plan_path = Path(summary["plan_path"])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            auto_decision = evaluate_auto_publish(
                plan,
                PROJECT_ROOT,
                max_items=args.max_auto_publish_items,
            )
            report, next_notification_state = build_reconciliation_report(
                summary=summary,
                plan=plan,
                project_root=PROJECT_ROOT,
                started_at=started_at.isoformat(timespec="seconds"),
                finished_at=planned_at,
                previous_state=load_notification_state(NOTIFICATION_STATE),
                auto_decision=auto_decision,
            )
            mode = execution_mode(auto_decision, dry_run=args.dry_run)
            return_code = 0
            if mode == "dry_run":
                report = {
                    **report,
                    "status": "dry_run",
                    "should_notify": False,
                }
                log.emit(
                    "auto_publish_dry_run",
                    eligible=auto_decision.get("eligible"),
                    publish_count=auto_decision.get("publish_count"),
                    blocker_count=len(auto_decision.get("blockers", [])),
                )
            elif mode == "execute":
                log.emit(
                    "auto_publish_started",
                    plan_path=str(plan_path),
                    plan_hash=plan.get("plan_hash"),
                    publish_scope_hash=auto_decision.get("publish_scope_hash"),
                    publish_count=auto_decision.get("publish_count"),
                )
                try:
                    execution = execute_auto_publish(
                        plan_path=plan_path,
                        plan=plan,
                        log=log,
                        max_snapshot_age_hours=args.max_youtube_snapshot_age_hours,
                    )
                except AutoPublishExecutionError as exc:
                    report = build_execution_failure_report(
                        report,
                        stage=exc.stage,
                        error=str(exc),
                        partial_publications=exc.partial_publications,
                        log_path=exc.ledger_path or str(log.path),
                        finished_at=datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    )
                    if exc.ledger_path:
                        report["ledger_path"] = exc.ledger_path
                    return_code = 1
                    log.emit(
                        "auto_publish_failed",
                        stage=exc.stage,
                        error=str(exc),
                        partial_publish_count=len(exc.partial_publications),
                        ledger_path=exc.ledger_path,
                    )
                else:
                    report = build_completion_report(
                        report,
                        execution,
                        finished_at=datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    )
                    log.emit(
                        "auto_publish_completed",
                        publish_count=report["publish_count"],
                        ledger_path=report.get("ledger_path"),
                        platform_results=report.get("platform_results"),
                    )

            finished_at = str(report.get("finished_at") or planned_at)
            heartbeat = {
                "started_at": started_at.isoformat(timespec="seconds"),
                "finished_at": finished_at,
                "status": report["status"],
                **summary,
                "publish_count": report.get("publish_count", 0),
                "auto_publish_eligible": auto_decision.get("eligible"),
                "auto_publish_mode": mode,
                "new_blocked_count": report.get("new_problem_count", 0),
                "unchanged_actionable_blocked_count": report[
                    "unchanged_actionable_blocked_count"
                ],
                "background_blocked_count": report["background_blocked_count"],
                "should_notify": report["should_notify"],
                "ledger_path": report.get("ledger_path"),
            }
            HEARTBEAT.write_text(
                json.dumps(heartbeat, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            log.emit("run_completed", **heartbeat)
            if notification_delivery_enabled(
                no_notification=args.no_notification,
                dry_run=args.dry_run,
            ):
                if report["status"] == "completed":
                    titles = "、".join(
                        str(item.get("title") or item.get("video_id"))
                        for item in report.get("publish_items", [])[:2]
                    )
                    notify("课代表播客已自动发布", titles)
                elif report["status"] == "action_required":
                    notify(
                        "课代表播客需要处理",
                        f"发现 {len(report.get('action_items', []))} 个具体问题；邮件中有处理位置。",
                    )
                elif report["status"] == "maintenance":
                    notify("课代表播客后台维护", "有非紧急维护事项，邮件仅提醒一次。")
                if report["should_notify"]:
                    email_result = notify_email(report)
                    log.emit("email_notification", **email_result)
                    if email_result.get("status") == "sent":
                        atomic_write_json(NOTIFICATION_STATE, next_notification_state)
                else:
                    # A successful quiet run may resolve an old problem. Persist
                    # that internal state so a later recurrence is treated as new.
                    atomic_write_json(NOTIFICATION_STATE, next_notification_state)
                    log.emit(
                        "email_notification_suppressed",
                        reason="no_new_content_or_action",
                        background_blocked_count=report["background_blocked_count"],
                        unchanged_actionable_blocked_count=report[
                            "unchanged_actionable_blocked_count"
                        ],
                    )
            rotate_logs(datetime.now(timezone.utc))
            return return_code
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
            if notification_delivery_enabled(
                no_notification=args.no_notification,
                dry_run=args.dry_run,
            ):
                notify("课代表播客同步失败", f"{type(exc).__name__}: {exc}")
                log.emit("email_notification", **notify_email(failure))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
