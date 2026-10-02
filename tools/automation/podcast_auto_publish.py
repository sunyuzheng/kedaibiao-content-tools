#!/usr/bin/env python3
"""Fail-closed eligibility checks for unattended podcast publication.

This module performs no network requests and no writes.  It checks the exact
immutable plan and every local artifact immediately before the existing
executor performs its own remote precondition/readback checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.podcast.core import (
    canonical_json,
    extract_video_id,
    plan_hash,
    sha256_file,
    sha256_text,
    timed_text_to_text,
)
from tools.podcast.show_notes import (
    PORTABLE_HTML_FORMAT,
    render_portable_show_notes_html,
    validate_show_notes,
)
from tools.podcast.promotion import (
    PROMOTION_HTML_FORMAT, render_promoted_show_notes_html, validate_promotion_source,
)


DEFAULT_MAX_AUTO_PUBLISH_ITEMS = 3
ALLOWED_ACTIONS = frozenset(
    {"create_draft_then_publish", "update_draft_then_publish"}
)
SOFT_WARNING_CODES = frozenset({"missing_transcript"})
SOFT_WARNING_PREFIXES = (
    "podcast_description_warning:",
    "description_warning:",
)


def _issue(
    code: str,
    *,
    context: str,
    impact: str,
    action: str,
    location: str,
    video_id: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "video_id": video_id,
        "context": context,
        "impact": impact,
        "action": action,
        "location": location,
    }


def _safe_project_path(project_root: Path, value: Any) -> Path | None:
    if not value:
        return None
    root = project_root.resolve()
    path = (root / str(value)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _publish_scope(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the executor's exact publish scope without renaming its schema."""
    return {
        "kind": "podcast_publish",
        "show_id": plan.get("show_id"),
        "youtube_snapshot": plan.get("youtube_snapshot"),
        "items": plan.get("publish_actions", []),
        "projected_feed": plan.get("projected_feed", []),
        "projected_reorder_actions": plan.get("projected_reorder_actions", []),
        "publish_blocked_reasons": plan.get("publish_blocked_reasons", []),
    }


def _fresh_verified_at(value: Any, max_age_hours: Any, now: datetime) -> bool:
    if not value:
        return False
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        limit = float(max_age_hours)
    except (TypeError, ValueError):
        return False
    age_hours = (now - observed.astimezone(timezone.utc)).total_seconds() / 3600
    return -0.25 <= age_hours <= limit


def _valid_datetime(value: Any) -> bool:
    if not value:
        return False
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _verify_audio(
    local: dict[str, Any],
    project_root: Path,
    video_id: str,
) -> list[dict[str, Any]]:
    path_value = local.get("audio_path")
    path = _safe_project_path(project_root, path_value)
    location = str(path_value or local.get("folder") or "archive/")
    if path is None or not path.is_file() or path.stat().st_size <= 0:
        return [
            _issue(
                "audio_missing",
                video_id=video_id,
                context=f"{video_id} 没有可验证的本地音频文件。",
                impact="不能安全创建或修复 Transistor 单集。",
                action="重新下载该视频的音频，再重跑同步。",
                location=location,
            )
        ]
    expected_hash = str(local.get("audio_sha256") or "")
    expected_bytes = local.get("audio_bytes")
    if (
        not expected_hash
        or sha256_file(path) != expected_hash
        or expected_bytes != path.stat().st_size
    ):
        return [
            _issue(
                "audio_artifact_changed",
                video_id=video_id,
                context=f"{video_id} 的音频 hash 或字节数与不可变计划不一致。",
                impact="计划生成后音频工件发生了变化，本次自动发布会停止。",
                action="确认本地音频正确后重新生成同步计划。",
                location=location,
            )
        ]
    return []


def _description_text(
    local: dict[str, Any],
    project_root: Path,
) -> tuple[str, str | None]:
    inline = local.get("description_text")
    path_value = local.get("description_path")
    if inline is not None:
        if path_value:
            raise ValueError("description has both inline text and source path")
        source = str(inline).strip()
        expected_source_hash = local.get("description_source_sha256")
        if expected_source_hash and sha256_text(source) != expected_source_hash:
            raise ValueError("inline description source hash changed")
        description_format = local.get("description_format")
        if description_format == PROMOTION_HTML_FORMAT:
            if not expected_source_hash:
                raise ValueError("promotion inline description is missing source hash")
            return render_promoted_show_notes_html(
                source, local.get("description_renderer_inputs"), project_root,
            ), source
        if description_format == PORTABLE_HTML_FORMAT:
            if not expected_source_hash:
                raise ValueError("portable inline description is missing source hash")
            return render_portable_show_notes_html(source), source
        if description_format:
            raise ValueError(f"unsupported description format: {description_format}")
        if expected_source_hash:
            raise ValueError("inline source hash requires a renderer")
        return source, source
    path = _safe_project_path(project_root, path_value)
    if path is None or not path.is_file():
        raise FileNotFoundError(str(path_value or "description source"))
    source = path.read_text(encoding="utf-8", errors="replace").strip()
    expected_source_hash = local.get("description_source_sha256")
    if expected_source_hash and sha256_text(source) != expected_source_hash:
        raise ValueError("description source hash changed")
    description_format = local.get("description_format")
    if description_format == PROMOTION_HTML_FORMAT:
        if not expected_source_hash:
            raise ValueError("promotion description is missing source hash")
        return render_promoted_show_notes_html(
            source, local.get("description_renderer_inputs"), project_root,
        ), source
    if description_format == PORTABLE_HTML_FORMAT:
        if not expected_source_hash:
            raise ValueError("portable description is missing source hash")
        return render_portable_show_notes_html(source), source
    if description_format:
        raise ValueError(f"unsupported description format: {description_format}")
    return source, source


def _verify_description(
    local: dict[str, Any],
    project_root: Path,
    video_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    location = str(
        local.get("description_path")
        or f"plan publish_actions[{video_id}].local.description_text"
    )
    try:
        description, source = _description_text(local, project_root)
    except (OSError, ValueError) as exc:
        return [
            _issue(
                "description_artifact_invalid",
                video_id=video_id,
                context=f"{video_id} 的 Show Notes 工件无法按计划复现：{exc}",
                impact="不能确认即将公开的描述与已校验 payload 完全相同。",
                action="修正描述来源后重新生成同步计划。",
                location=location,
            )
        ], []
    if not description:
        return [
            _issue(
                "description_missing",
                video_id=video_id,
                context=f"{video_id} 没有可公开的 Show Notes。",
                impact="节目发现、搜索与品牌信息都会缺失，因此不自动发布。",
                action="补充 YouTube/local description 或 podcast sidecar 后重跑同步。",
                location=(
                    f"podcast_show_notes/{video_id}.txt 或 "
                    f"{local.get('folder') or 'archive/'}"
                ),
            )
        ], []
    if (
        sha256_text(description) != local.get("description_sha256")
        or len(description) != local.get("description_chars")
    ):
        return [
            _issue(
                "description_artifact_changed",
                video_id=video_id,
                context=f"{video_id} 的 Show Notes hash 或字符数与不可变计划不一致。",
                impact="计划生成后公开描述发生了变化，本次自动发布会停止。",
                action="确认描述正确后重新生成同步计划。",
                location=location,
            )
        ], []

    validation_source = source if local.get("description_format") in {PORTABLE_HTML_FORMAT, PROMOTION_HTML_FORMAT} else description
    quality = (
        validate_promotion_source(validation_source)
        if local.get("description_format") == PROMOTION_HTML_FORMAT
        else validate_show_notes(validation_source if validation_source is not None else description)
    )
    errors = list(quality.get("errors", []))
    if len(description) > 10_000 and "rendered_over_10000_chars" not in errors:
        errors.append("rendered_over_10000_chars")
    if errors:
        return [
            _issue(
                "description_validation_failed",
                video_id=video_id,
                context=f"{video_id} 的 Show Notes 有硬错误：{', '.join(errors)}。",
                impact="描述可能被截断、含无效时间戳或不安全占位符，因此不自动发布。",
                action="按 validator 结果修正描述，再重跑同步。",
                location=location,
            )
        ], []

    receipts: list[dict[str, Any]] = []
    if local.get("description_source") != "podcast_sidecar":
        receipts.append(
            {
                "video_id": video_id,
                "warning": "show_notes_fallback_source",
                "detail": (
                    "本期使用现有 YouTube/local description 自动发布；"
                    f"后续可在 podcast_show_notes/{video_id}.txt 原位优化。"
                ),
                "source": local.get("description_source"),
            }
        )
    receipts.extend(
        {
            "video_id": video_id,
            "warning": str(warning),
            "detail": f"Show Notes 质量提示：{warning}",
            "source": local.get("description_source"),
        }
        for warning in quality.get("warnings", [])
    )
    return [], receipts


def _verify_optional_transcript(
    local: dict[str, Any],
    project_root: Path,
    video_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path_value = local.get("transcript_path")
    if not path_value:
        return [], [
            {
                "video_id": video_id,
                "warning": "missing_transcript",
                "detail": "本期没有文字稿；按现行政策不阻止音频发布。",
            }
        ]
    path = _safe_project_path(project_root, path_value)
    if path is None or not path.is_file():
        return [
            _issue(
                "transcript_artifact_missing",
                video_id=video_id,
                context=f"{video_id} 的计划引用了文字稿，但文件已不存在。",
                impact="引用工件不一致；为避免写入错误内容，本次自动发布停止。",
                action="恢复文字稿或移除失效引用后重新生成计划。",
                location=str(path_value),
            )
        ], []
    transcript = timed_text_to_text(path)
    if (
        not local.get("transcript_sha256")
        or sha256_text(transcript) != local.get("transcript_sha256")
        or len(transcript) != local.get("transcript_chars")
    ):
        return [
            _issue(
                "transcript_artifact_changed",
                video_id=video_id,
                context=f"{video_id} 的文字稿与不可变计划不一致。",
                impact="引用工件发生变化，本次自动发布停止。",
                action="确认文字稿后重新生成同步计划。",
                location=str(path_value),
            )
        ], []
    return [], []


def evaluate_auto_publish(
    plan: dict[str, Any],
    project_root: Path,
    *,
    max_items: int = DEFAULT_MAX_AUTO_PUBLISH_ITEMS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic, human-actionable auto-publication decision."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    actions = list(plan.get("publish_actions", []))
    has_candidates = bool(actions)
    blockers: list[dict[str, Any]] = []
    receipt_warnings: list[dict[str, Any]] = []

    expected_plan_hash = plan.get("plan_hash")
    actual_plan_hash = plan_hash(plan)
    if not expected_plan_hash or expected_plan_hash != actual_plan_hash:
        blockers.append(
            _issue(
                "plan_integrity_mismatch",
                context="不可变同步计划的 plan hash 校验失败。",
                impact="无法证明执行内容就是生成时审计过的内容。",
                action="删除本次执行念头并重新生成计划；不要手改 JSON。",
                location="logs/podcast_sync/plans/latest.json",
            )
        )

    scope_hash = sha256_text(canonical_json(_publish_scope(plan)))
    embedded_scope_hash = plan.get("publish_approval_hash")
    if not embedded_scope_hash or embedded_scope_hash != scope_hash:
        blockers.append(
            _issue(
                "publish_scope_integrity_mismatch",
                context="发布 scope hash 与计划中的发布 payload 不一致。",
                impact="执行器无法锁定确切发布目标、受众与重排范围。",
                action="重新生成计划；不要复制旧 hash 到新计划。",
                location="logs/podcast_sync/plans/latest.json",
            )
        )

    if max_items < 1:
        raise ValueError("max_items must be at least 1")
    if max_items > DEFAULT_MAX_AUTO_PUBLISH_ITEMS:
        raise ValueError(
            "max_items cannot exceed the hard auto-publish safety limit "
            f"of {DEFAULT_MAX_AUTO_PUBLISH_ITEMS}"
        )
    if len(actions) > max_items:
        blockers.append(
            _issue(
                "auto_publish_batch_limit_exceeded",
                context=f"本次计划包含 {len(actions)} 期，超过自动发布上限 {max_items} 期。",
                impact="这更像积压回填而不是日常增量，批量误操作影响面过大。",
                action="在 Codex 中审阅该计划并拆分或按明确 payload 手动执行。",
                location="logs/podcast_sync/plans/latest.json",
            )
        )

    snapshot = plan.get("youtube_snapshot") or {}
    if not snapshot.get("fresh"):
        blockers.append(
            _issue(
                "youtube_snapshot_stale",
                context="YouTube 当前公共清单证据已过期。",
                impact="无法确认候选仍为公开的新一期，因此不会自动发布。",
                action=(
                    "先运行 `.venv-podcast/bin/python tools/automation/sync_podcast.py "
                    "--dry-run --force-candidate-verification`；若 OAuth 也失效，运行 "
                    "`envs/youtube_env/bin/python tools/youtube/fetch_all_videos.py "
                    "--expected-channel-id UC_5lJHgnMP_lb_VpIiXV0hQ` 并完成登录。"
                ),
                location="tools/youtube/public_videos_snapshot.json",
            )
        )
    verification_info = snapshot.get("candidate_verification") or {}
    if actions and not verification_info.get("fresh"):
        blockers.append(
            _issue(
                "candidate_verification_stale",
                context="本期候选的 bounded public verification 已过期或缺失。",
                impact="不能确认这些具体视频此刻仍公开可访问。",
                action=(
                    "运行 `.venv-podcast/bin/python tools/automation/sync_podcast.py "
                    "--dry-run --force-candidate-verification`。"
                ),
                location="tools/youtube/podcast_candidate_verification.json",
            )
        )
    if plan.get("publish_blocked_reasons"):
        blockers.append(
            _issue(
                "global_publish_precondition_failed",
                context=(
                    "全局发布/排序前置条件不满足："
                    + ", ".join(str(item) for item in plan["publish_blocked_reasons"])
                ),
                impact="发布可能造成整个公开 feed 的编号或标题错误。",
                action="打开计划中的 publish_blocked_reasons，修复本地日期或重复映射后重建计划。",
                location="logs/podcast_sync/plans/latest.json",
            )
        )

    reorder_actions = list(plan.get("projected_reorder_actions", []))
    historical_reorders = [
        item for item in reorder_actions if not item.get("planned_publish")
    ]
    if historical_reorders:
        blockers.append(
            _issue(
                "historical_episode_reorder_not_automatic",
                context=(
                    f"计划会改动 {len(historical_reorders)} 个历史 episode 的编号或标题。"
                ),
                impact="日常自动发布不能顺带改写既有公开节目。",
                action="在 Codex 中审阅完整 reorder diff；确认后再单独执行。",
                location="logs/podcast_sync/plans/latest.json → projected_reorder_actions",
            )
        )

    baseline = plan.get("incremental_publish_baseline") or {}
    if actions and not baseline.get("available"):
        blockers.append(
            _issue(
                "incremental_baseline_unavailable",
                context="找不到“最新已发布 YouTube 位置”的可信增量基线。",
                impact="无法区分新一期与历史漏档。",
                action="核对最新已发布 Transistor episode 的 YouTube URL，再重建计划。",
                location="Transistor Dashboard → 课代表立正 → Episodes",
            )
        )

    try:
        baseline_position = int(baseline.get("playlist_index"))
    except (TypeError, ValueError):
        baseline_position = 0

    for item in actions:
        local = item.get("local") or {}
        video_id = str(local.get("video_id") or "")
        folder = str(local.get("folder") or "archive/")
        if item.get("action") not in ALLOWED_ACTIONS:
            blockers.append(
                _issue(
                    "unexpected_publish_action",
                    video_id=video_id or None,
                    context=f"计划包含不受支持的自动动作：{item.get('action')}。",
                    impact="该动作未经过日常增量发布策略验证。",
                    action="在 Codex 中审阅并手动处理该 scope。",
                    location="logs/podcast_sync/plans/latest.json",
                )
            )
        if (
            local.get("youtube_privacy") != "public"
            or local.get("content_class") != "normal_video"
            or local.get("podcast_policy") != "ready_public_normal"
        ):
            blockers.append(
                _issue(
                    "candidate_policy_not_public_normal",
                    video_id=video_id or None,
                    context=(
                        f"{video_id or '候选'} 的 canonical policy 不是 public + normal_video "
                        f"（privacy={local.get('youtube_privacy')}, "
                        f"class={local.get('content_class')}, "
                        f"policy={local.get('podcast_policy')}）。"
                    ),
                    impact="会员、直播回放或非公开视频不能进入日常播客自动发布。",
                    action="核对对应 info.json 与 YouTube 可见性；不要仅修改计划 JSON。",
                    location=folder,
                )
            )

        if not str(local.get("base_title") or "").strip():
            blockers.append(
                _issue(
                    "candidate_title_missing",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 没有可公开的标题。",
                    impact="无法生成稳定的 episode 标题与发布回执。",
                    action="补齐 canonical info.json 的 title 后重建计划。",
                    location=folder,
                )
            )
        if not video_id or extract_video_id(local.get("video_url")) != video_id:
            blockers.append(
                _issue(
                    "candidate_video_url_mismatch",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 的 video_url 不能精确解析回同一个 YouTube ID。",
                    impact="远端幂等映射与发布后核验不可靠。",
                    action="修正 canonical video_id/URL 来源后重新生成计划；不要手改计划。",
                    location=folder,
                )
            )
        if video_id and video_id not in str(local.get("image_url") or ""):
            blockers.append(
                _issue(
                    "candidate_image_url_missing",
                    video_id=video_id,
                    context=f"{video_id} 没有与本期 ID 对应的封面 URL。",
                    impact="发布后节目封面可能为空或指向错误视频。",
                    action="修正 canonical image URL 来源后重新生成计划。",
                    location=folder,
                )
            )
        try:
            position = int(local.get("playlist_index"))
        except (TypeError, ValueError):
            position = 0
        if not baseline_position or not position or position >= baseline_position:
            blockers.append(
                _issue(
                    "candidate_not_incremental",
                    video_id=video_id or None,
                    context=(
                        f"{video_id or '候选'} 不在可信基线之前的新增区间 "
                        f"（candidate={position or 'unknown'}, baseline={baseline_position or 'unknown'}）。"
                    ),
                    impact="它可能是历史漏档，不能由日常任务自动补发。",
                    action="如确需补档，在 Codex 中启用并审阅单独 backfill scope。",
                    location="logs/podcast_sync/plans/latest.json",
                )
            )

        verification = item.get("youtube_verification") or {}
        max_age = verification_info.get(
            "max_age_hours", snapshot.get("max_age_hours", 72)
        )
        if verification_info.get("fresh") and (
            verification.get("status") != "public"
            or not _fresh_verified_at(verification.get("verified_at"), max_age, now)
        ):
            blockers.append(
                _issue(
                    "candidate_not_freshly_verified_public",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 没有新鲜的逐条 public 验证。",
                    impact="不能确认该具体视频当前仍公开。",
                    action=(
                        "运行 `.venv-podcast/bin/python tools/automation/sync_podcast.py "
                        "--dry-run --force-candidate-verification`。"
                    ),
                    location="tools/youtube/podcast_candidate_verification.json",
                )
            )

        warning_codes = [str(warning) for warning in item.get("warnings", [])]
        soft_warnings = [
            warning
            for warning in warning_codes
            if warning in SOFT_WARNING_CODES
            or warning.startswith(SOFT_WARNING_PREFIXES)
        ]
        # Fail closed: a future warning is hard until explicitly reviewed and
        # added to the soft allowlist above.
        hard_warnings = [
            warning for warning in warning_codes if warning not in soft_warnings
        ]
        if hard_warnings:
            blockers.append(
                _issue(
                    "candidate_has_hard_quality_warning",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 缺少发布必需内容：{', '.join(hard_warnings)}。",
                    impact="音频、日期或可公开描述不完整，本次不会自动发布。",
                    action="按 warning 补齐来源文件，再重新生成计划。",
                    location=folder,
                )
            )
        receipt_warnings.extend(
            {
                "video_id": video_id,
                "warning": str(warning),
                "detail": f"非阻塞质量提示：{warning}",
            }
            for warning in soft_warnings
        )

        remote = item.get("remote_precondition")
        if item.get("action") == "create_draft_then_publish" and remote is not None:
            blockers.append(
                _issue(
                    "create_remote_precondition_invalid",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 的创建计划却带有远端 episode 前置。",
                    impact="远端身份不明确，执行器不能安全幂等。",
                    action="重新获取 Transistor 状态并重建计划。",
                    location="Transistor Dashboard → 课代表立正 → Episodes",
                )
            )
        if item.get("action") == "update_draft_then_publish" and (
            not isinstance(remote, dict)
            or remote.get("status") != "draft"
            or not remote.get("episode_id")
        ):
            blockers.append(
                _issue(
                    "draft_remote_precondition_invalid",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 没有锁定唯一 draft episode。",
                    impact="可能更新错远端草稿，本次不会自动发布。",
                    action="在 Transistor Dashboard 合并/删除重复草稿，再重跑同步。",
                    location=(
                        "Transistor Dashboard → 课代表立正 → Episodes，搜索 "
                        f"{video_id or '对应 YouTube ID'}"
                    ),
                )
            )

        blockers.extend(_verify_audio(local, project_root, video_id))
        description_blockers, description_receipts = _verify_description(
            local, project_root, video_id
        )
        blockers.extend(description_blockers)
        receipt_warnings.extend(description_receipts)
        transcript_blockers, transcript_receipts = _verify_optional_transcript(
            local, project_root, video_id
        )
        blockers.extend(transcript_blockers)
        receipt_warnings.extend(transcript_receipts)
        if not _valid_datetime(local.get("published_at")):
            blockers.append(
                _issue(
                    "publish_date_missing",
                    video_id=video_id or None,
                    context=f"{video_id or '候选'} 没有可信发布日期。",
                    impact="不能安全决定公开时间与 episode 顺序。",
                    action="补齐对应 info.json 的 upload_date 后重建计划。",
                    location=folder,
                )
            )

    unique_blockers: dict[str, dict[str, Any]] = {}
    for blocker in blockers:
        key = canonical_json(
            {
                "code": blocker.get("code"),
                "video_id": blocker.get("video_id"),
                "context": blocker.get("context"),
            }
        )
        unique_blockers[key] = blocker
    unique_receipts: dict[str, dict[str, Any]] = {}
    for warning in receipt_warnings:
        key = canonical_json(
            {
                "video_id": warning.get("video_id"),
                "warning": warning.get("warning"),
            }
        )
        unique_receipts[key] = warning

    blockers = list(unique_blockers.values())
    return {
        "has_candidates": has_candidates,
        "eligible": bool(has_candidates and not blockers),
        "publish_count": len(actions),
        "max_auto_publish_items": max_items,
        "plan_hash": expected_plan_hash,
        # The executor's legacy field name remains schema-compatible; this
        # workflow treats it only as a payload integrity lock.
        "publish_scope_hash": scope_hash,
        "blockers": blockers,
        "receipt_warnings": list(unique_receipts.values()),
    }
