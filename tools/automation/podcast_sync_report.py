#!/usr/bin/env python3
"""Build useful, de-duplicated owner reports for podcast synchronization."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.podcast.core import canonical_json, sha256_text


SHOW_NAME = "课代表立正"
PUBLIC_FEED_URL = "https://feeds.transistor.fm/kedaibiao"
BACKGROUND_BLOCK_REASONS = frozenset({"historical_gap_requires_backfill_mode"})


class NotificationReportError(RuntimeError):
    pass


def _stable_key(item: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(
            {
                "code": item.get("code"),
                "video_id": item.get("video_id"),
                "context": item.get("context"),
                "action": item.get("action"),
                "location": item.get("location"),
            }
        )
    )


def _semantic_key(item: dict[str, Any]) -> tuple[str, str]:
    """Collapse planner/auto-gate descriptions of the same real problem."""
    code = str(item.get("code") or "unknown")
    family_by_code = {
        "plan_blocker:stale_youtube_snapshot": "youtube_snapshot_stale",
        "youtube_snapshot_stale": "youtube_snapshot_stale",
        "plan_blocker:youtube_candidate_not_verified": "candidate_verification",
        "candidate_verification_stale": "candidate_verification",
        "candidate_not_freshly_verified_public": "candidate_verification",
        "plan_blocker:no_audio_for_create_or_repair": "audio_missing",
        "audio_missing": "audio_missing",
        "plan_blocker:missing_publish_date": "publish_date_missing",
        "publish_date_missing": "publish_date_missing",
    }
    if code.startswith("plan_blocker:published_episode_missing_local_date"):
        family = "global_publish_precondition"
    elif code.startswith("plan_blocker:duplicate_published_video_ids"):
        family = "global_publish_precondition"
    elif code == "global_publish_precondition_failed":
        family = "global_publish_precondition"
    else:
        family = family_by_code.get(code, code)
    return str(item.get("video_id") or "global"), family


def _is_background_blocker(item: dict[str, Any]) -> bool:
    reasons = {str(reason) for reason in item.get("reasons", [])}
    return bool(reasons) and reasons <= BACKGROUND_BLOCK_REASONS


def _description_source_text(local: dict[str, Any], project_root: Path) -> str:
    inline = local.get("description_text")
    if inline is not None:
        return str(inline).strip()
    path_value = local.get("description_path")
    if not path_value:
        return ""
    path = (project_root / str(path_value)).resolve()
    try:
        path.relative_to(project_root.resolve())
    except ValueError as exc:
        raise NotificationReportError(
            f"Description source escapes project root: {path_value}"
        ) from exc
    if not path.is_file():
        raise NotificationReportError(f"Description source is missing: {path_value}")
    source = path.read_text(encoding="utf-8", errors="replace").strip()
    expected = local.get("description_source_sha256")
    if expected and sha256_text(source) != expected:
        raise NotificationReportError(
            f"Description source changed after plan creation: {path_value}"
        )
    return source


def _publish_items(plan: dict[str, Any], project_root: Path) -> list[dict[str, Any]]:
    targets = {
        row.get("video_id"): row
        for row in plan.get("projected_feed", [])
        if row.get("planned_publish")
    }
    items: list[dict[str, Any]] = []
    for action in plan.get("publish_actions", []):
        local = action.get("local", {})
        video_id = str(local.get("video_id") or "")
        target = targets.get(video_id, {})
        items.append(
            {
                "action": action.get("action"),
                "video_id": video_id,
                "episode_number": target.get("target_number"),
                "title": target.get("target_title") or local.get("base_title"),
                "published_at": local.get("published_at"),
                "youtube_url": local.get("video_url"),
                "image_url": local.get("image_url"),
                "description": _description_source_text(local, project_root),
                "description_source": local.get("description_source"),
                "description_format": local.get("description_format"),
                "description_chars": local.get("description_chars"),
                "description_sha256": local.get("description_sha256"),
                "transcript_source_status": local.get("transcript_source_status"),
                "transcript_chars": local.get("transcript_chars"),
                "transcript_sha256": local.get("transcript_sha256"),
                "audio_bytes": local.get("audio_bytes"),
                "audio_sha256": local.get("audio_sha256"),
                "youtube_verified_at": (
                    action.get("youtube_verification", {}).get("verified_at")
                ),
                "warnings": list(action.get("warnings", [])),
            }
        )
    return items


def _raw_reason_issue(
    reason: str,
    item: dict[str, Any],
    *,
    plan_path: str,
    show_id: Any,
) -> dict[str, Any]:
    video_id = str(item.get("video_id") or "") or None
    title = str(item.get("title") or video_id or "相关单集")
    dashboard = "Transistor Dashboard → 课代表立正 → Episodes"
    common: dict[str, tuple[str, str, str, str]] = {
        "stale_youtube_snapshot": (
            "YouTube 公共清单证据已过期。",
            "不能确认待发布视频当前仍公开。",
            "运行 `.venv-podcast/bin/python tools/automation/sync_podcast.py --dry-run --force-candidate-verification`；如要求登录，再运行 interactive YouTube refresh。",
            "tools/youtube/public_videos_snapshot.json",
        ),
        "youtube_candidate_not_verified": (
            f"{title} 没有通过本期逐条 public 验证。",
            "本期不会自动发布，以免误发会员或不可见内容。",
            "重跑 `sync_podcast.py --dry-run --force-candidate-verification`；若仍失败，检查 YouTube 可见性。",
            "tools/youtube/podcast_candidate_verification.json",
        ),
        "multiple_remote_drafts": (
            f"{title} 在 Transistor 有多个 draft。",
            "无法唯一锁定要更新和发布的远端 episode。",
            f"在 Transistor 搜索 YouTube ID {video_id or ''}，保留唯一正确 draft 后重跑同步。",
            dashboard,
        ),
        "no_audio_for_create_or_repair": (
            f"{title} 没有可用于创建或修复的音频。",
            "Transistor 无法生成可播放的新一期。",
            "重新下载本期音频并确认文件非空，然后重跑同步。",
            "archive/ 对应视频目录",
        ),
        "missing_publish_date": (
            f"{title} 缺少可信发布日期。",
            "不能安全设置公开时间与 episode 顺序。",
            "补齐对应 info.json 的 upload_date 后重建计划。",
            "archive/ 对应视频目录/*.info.json",
        ),
        "incremental_baseline_unavailable": (
            "找不到最新已发布 episode 的可信 YouTube 增量基线。",
            "无法区分真正新一期与历史漏档。",
            "核对最新已发布 episode 的 YouTube URL，再重建计划。",
            dashboard,
        ),
        "incremental_position_unavailable": (
            f"{title} 在当前 YouTube uploads 清单里没有稳定位置。",
            "无法证明它是基线之后的新一期。",
            "刷新公共清单与候选验证后重建计划。",
            "tools/youtube/public_videos_snapshot.json",
        ),
        "multiple_remote_published_episodes": (
            f"{title} 对应多个 published episode。",
            "Show Notes/文字稿更新无法安全选定唯一目标。",
            f"在 Transistor 搜索 YouTube ID {video_id or ''}，确认唯一 canonical episode。",
            dashboard,
        ),
    }
    if reason.startswith("invalid_podcast_description:"):
        detail = reason.split(":", 1)[1]
        context, impact, action, location = (
            f"{title} 的 podcast sidecar 未通过校验：{detail}。",
            "这只阻止该 Show Notes 原位更新；不会阻止另一条合格新一期发布。",
            "修正 sidecar 后重新生成计划。",
            f"podcast_show_notes/{video_id}.txt" if video_id else "podcast_show_notes/",
        )
    else:
        context, impact, action, location = common.get(
            reason,
            (
                f"{title} 有一项同步前置条件尚未满足。",
                "相关自动写入会保持停止，不影响其他已独立通过校验的 scope。",
                "在 Codex 中打开本次计划与日志，确认具体来源后再处理。",
                plan_path,
            ),
        )
    return {
        "code": f"plan_blocker:{reason}",
        "video_id": video_id,
        "context": context,
        "impact": impact,
        "action": action,
        "location": location,
        "show_id": show_id,
    }


def _maintenance_items(plan: dict[str, Any], plan_path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    snapshot = plan.get("youtube_snapshot", {})
    oauth = snapshot.get("oauth_snapshot", {})
    if oauth.get("exists") and not oauth.get("fresh") and snapshot.get("fresh"):
        age = oauth.get("age_hours")
        age_text = f"约 {round(float(age) / 24)} 天" if age is not None else "较久"
        items.append(
            {
                "code": "maintenance:youtube_oauth_snapshot_stale",
                "context": f"YouTube OAuth 全量快照已旧（{age_text}），当前匿名公共清单仍新鲜。",
                "impact": "不影响本次 public + normal 新一期发布；仅影响需要 OAuth 的全量元数据维护。",
                "action": (
                    "方便时运行 `envs/youtube_env/bin/python tools/youtube/fetch_all_videos.py "
                    "--expected-channel-id UC_5lJHgnMP_lb_VpIiXV0hQ` 并按浏览器提示登录。"
                ),
                "location": "tools/youtube/all_videos_full.json",
            }
        )
    description_actions = list(plan.get("description_actions", []))
    if description_actions:
        items.append(
            {
                "code": "maintenance:description_actions_pending",
                "context": f"有 {len(description_actions)} 期已发布节目存在明确的 sidecar Show Notes 更新。",
                "impact": "它们与新一期发布分开，不会被日常自动发布顺带改写。",
                "action": "在 Codex 中审阅 description scope 的确切 diff 后单独执行。",
                "location": f"{plan_path} → description_actions",
            }
        )
    transcript_actions = list(plan.get("transcript_actions", []))
    if transcript_actions:
        items.append(
            {
                "code": "maintenance:transcript_actions_pending",
                "context": f"有 {len(transcript_actions)} 期已发布节目可回填文字稿。",
                "impact": "它们与新一期发布分开，不会阻止日常自动发布。",
                "action": "在 Codex 中审阅 transcript scope 后按需执行。",
                "location": f"{plan_path} → transcript_actions",
            }
        )
    return items


def build_reconciliation_report(
    *,
    summary: dict[str, Any],
    plan: dict[str, Any],
    project_root: Path,
    started_at: str,
    finished_at: str,
    previous_state: dict[str, Any] | None = None,
    auto_decision: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the pre-execution report and the state persisted after delivery."""
    if summary.get("plan_hash") != plan.get("plan_hash"):
        raise NotificationReportError("Summary and immutable plan hashes differ")
    previous_state = previous_state or {}
    auto_decision = auto_decision or {
        "has_candidates": False,
        "eligible": False,
        "blockers": [],
        "receipt_warnings": [],
    }
    plan_path = str(summary.get("plan_path") or "")

    blocked = list(plan.get("blocked", []))
    background = [item for item in blocked if _is_background_blocker(item)]
    actionable_raw = [item for item in blocked if not _is_background_blocker(item)]
    action_items: list[dict[str, Any]] = []
    for item in actionable_raw:
        for reason in item.get("reasons", []):
            action_items.append(
                _raw_reason_issue(
                    str(reason), item, plan_path=plan_path, show_id=plan.get("show_id")
                )
            )
    for reason in plan.get("publish_blocked_reasons", []):
        action_items.append(
            _raw_reason_issue(
                str(reason),
                {"scope": "publish", "title": "公开 feed"},
                plan_path=plan_path,
                show_id=plan.get("show_id"),
            )
        )
    action_items.extend(auto_decision.get("blockers", []))
    # Auto-gate issues come last and therefore replace lower-level planner
    # wording for the same video + semantic problem.
    action_items = list({_semantic_key(item): item for item in action_items}.values())
    current_actions = {_stable_key(item): item for item in action_items}
    previous_action_keys = {
        str(value)
        for value in previous_state.get(
            "actionable_problem_keys",
            previous_state.get("actionable_blocker_keys", []),
        )
    }
    new_action_keys = set(current_actions) - previous_action_keys

    maintenance_items = _maintenance_items(plan, plan_path)
    current_maintenance = {_stable_key(item): item for item in maintenance_items}
    previous_maintenance_keys = {
        str(value) for value in previous_state.get("maintenance_keys", [])
    }
    new_maintenance_keys = set(current_maintenance) - previous_maintenance_keys

    new_action_items = [current_actions[key] for key in sorted(new_action_keys)]
    new_maintenance_items = [
        current_maintenance[key] for key in sorted(new_maintenance_keys)
    ]
    if new_action_items:
        status = "action_required"
    elif new_maintenance_items:
        status = "maintenance"
    elif auto_decision.get("eligible"):
        status = "ready_for_auto_publish"
    elif auto_decision.get("has_candidates"):
        status = "known_issue"
    else:
        status = "healthy"

    publish_items = _publish_items(plan, project_root)
    report = {
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "show_name": SHOW_NAME,
        "show_id": plan.get("show_id"),
        "feed_url": PUBLIC_FEED_URL,
        "audience": "Transistor 公共节目及其 RSS 下游（包括小宇宙等客户端）",
        "plan_hash": plan.get("plan_hash"),
        "plan_path": plan_path,
        "publish_scope_hash": auto_decision.get("publish_scope_hash"),
        "publish_count": len(publish_items),
        "description_count": len(plan.get("description_actions", [])),
        "transcript_count": len(plan.get("transcript_actions", [])),
        "publish_items": publish_items,
        "youtube_snapshot_fresh": bool(
            plan.get("youtube_snapshot", {}).get("fresh")
        ),
        "auto_publish_eligible": bool(auto_decision.get("eligible")),
        "max_auto_publish_items": auto_decision.get("max_auto_publish_items"),
        "action_items": new_action_items,
        "current_action_problem_count": len(current_actions),
        "new_problem_count": len(new_action_items),
        "unchanged_actionable_blocked_count": len(
            set(current_actions) & previous_action_keys
        ),
        # Historical gaps stay in machine heartbeat data only. They are never
        # rendered in owner email unless a separate backfill scope is requested.
        "background_blocked_count": len(background),
        "maintenance_items": new_maintenance_items,
        "receipt_warnings": list(auto_decision.get("receipt_warnings", [])),
        "should_notify": bool(new_action_items or new_maintenance_items),
    }
    next_state = {
        "schema_version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actionable_problem_keys": sorted(current_actions),
        "maintenance_keys": sorted(current_maintenance),
        "last_plan_hash": plan.get("plan_hash"),
    }
    return report, next_state


def build_completion_report(
    base_report: dict[str, Any],
    execution: dict[str, Any],
    *,
    finished_at: str,
) -> dict[str, Any]:
    """Create a completion receipt only after every required post-check passes."""
    published_by_video = {
        str(item.get("video_id")): item
        for item in execution.get("published_items", [])
    }
    items: list[dict[str, Any]] = []
    for planned in base_report.get("publish_items", []):
        actual = published_by_video.get(str(planned.get("video_id")), {})
        items.append({**planned, **actual})
    return {
        **base_report,
        "status": "completed",
        "finished_at": finished_at,
        "publish_items": items,
        "publish_count": len(items),
        "ledger_path": execution.get("ledger_path"),
        "post_publish_checks": execution.get("post_publish_checks", {}),
        "platform_results": execution.get("platform_results", {}),
        "should_notify": True,
    }


def build_execution_failure_report(
    base_report: dict[str, Any],
    *,
    stage: str,
    error: str,
    partial_publications: list[dict[str, Any]],
    log_path: str,
    finished_at: str,
) -> dict[str, Any]:
    """Report a failed/partial attempt without ever claiming completion."""
    stage_label = {
        "publish_executor": "Transistor 写入或远端读回",
        "publish_receipt": "执行回执核对",
        "per_episode_quality": "逐集发布质量检查",
        "reorder_check": "发布后顺序检查",
        "rebuild_plan": "发布后重建对账计划",
    }.get(stage, "自动发布检查")
    action = {
        "code": f"auto_publish_execution_failed:{stage}",
        "context": (
            f"自动发布在“{stage_label}”阶段失败。"
            + (
                f" 已确认其中 {len(partial_publications)} 期已发布，不能当作整批回滚。"
                if partial_publications
                else " 尚未确认有 episode 完成发布。"
            )
        ),
        "impact": "本次不会发送“发布完成”通知；需要先核对远端实际状态。",
        "action": (
            "打开执行 ledger/日志并在 Transistor Dashboard 核对列出的 episode；"
            "修复后重新生成计划。若需撤回，可在 Transistor 将该 episode 改回 Draft，"
            "但已被下游抓取或缓存的副本不保证同步消失。"
        ),
        "location": log_path,
    }
    return {
        **base_report,
        "status": "action_required",
        "finished_at": finished_at,
        "execution_stage": stage,
        "error": error,
        "log_path": log_path,
        "partial_publications": partial_publications,
        "action_items": [action, *base_report.get("action_items", [])],
        "should_notify": True,
    }


def load_notification_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
