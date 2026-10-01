#!/usr/bin/env python3
"""Best-effort Resend notifications for scheduled podcast synchronization."""

from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


RESEND_ENDPOINT = "https://api.resend.com/emails"
REPLY_TO = "sunyuzheng@gmail.com"


@dataclass(frozen=True)
class EmailConfig:
    api_key: str
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(cls) -> tuple[EmailConfig | None, list[str]]:
        values = {
            "RESEND_API_KEY": os.environ.get("RESEND_API_KEY", "").strip(),
            "RESEND_FROM_EMAIL": os.environ.get("RESEND_FROM_EMAIL", "").strip(),
            "PODCAST_SYNC_EMAIL_TO": os.environ.get(
                "PODCAST_SYNC_EMAIL_TO", ""
            ).strip(),
        }
        present = [name for name, value in values.items() if value]
        if not present:
            return None, []
        missing = [name for name, value in values.items() if not value]
        if missing:
            return None, missing
        recipients = tuple(
            item.strip()
            for item in values["PODCAST_SYNC_EMAIL_TO"].split(",")
            if item.strip()
        )
        if not recipients:
            return None, ["PODCAST_SYNC_EMAIL_TO"]
        return cls(
            api_key=values["RESEND_API_KEY"],
            sender=values["RESEND_FROM_EMAIL"],
            recipients=recipients,
        ), []


def _subject_and_headline(report: dict[str, Any]) -> tuple[str, str]:
    status = str(report.get("status") or "unknown")
    items = list(report.get("publish_items", []))
    titles = [str(item.get("title") or item.get("video_id")) for item in items]
    if status == "completed":
        suffix = "、".join(titles[:2])
        if len(titles) == 1:
            subject = f"[课代表播客] 已自动发布：{suffix}"
        else:
            subject = f"[课代表播客] 已自动发布 {len(titles)} 期：{suffix}"
        if report.get("action_items"):
            subject += "（另有事项需处理）"
        return subject, "发布、远端读回与发布后检查均已完成"
    if status == "action_required":
        count = max(1, len(report.get("action_items", [])))
        return (
            f"[课代表播客] 需要处理：{count} 项具体问题",
            "自动流程已安全停止；下面是具体影响与处理位置",
        )
    if status == "maintenance":
        count = len(report.get("maintenance_items", []))
        return (
            f"[课代表播客] 后台维护：{count} 项非紧急事项",
            "本次无需阻止发布；以下维护事项仅提醒一次",
        )
    if status == "failed":
        return (
            f"[课代表播客] 同步失败：{report.get('error_type') or 'unknown error'}",
            "同步在生成安全计划前失败，需要查看日志",
        )
    return "[课代表播客] 同步状态", "播客同步状态"


def _clean(value: Any, limit: int = 2000) -> str:
    return str(value if value is not None else "unknown").replace(
        "\r", " "
    ).replace("\n", " ")[:limit]


def _html_table(rows: list[tuple[str, Any]]) -> str:
    rendered: list[str] = []
    for label, value in rows:
        rendered.append(
            "<tr>"
            f'<th style="text-align:left;vertical-align:top;padding:6px 12px 6px 0;color:#52525b">{html.escape(label)}</th>'
            f'<td style="padding:6px 0;color:#18181b">{html.escape(_clean(value))}</td>'
            "</tr>"
        )
    return '<table style="border-collapse:collapse">' + "".join(rendered) + "</table>"


def _append_action_sections(
    report: dict[str, Any],
    text_lines: list[str],
    html_sections: list[str],
) -> None:
    action_items = list(report.get("action_items", []))
    if action_items:
        text_lines += ["", "需要处理"]
        cards: list[str] = []
        for index, item in enumerate(action_items, 1):
            rows = [
                ("发生了什么", item.get("context")),
                ("影响", item.get("impact")),
                ("你要做什么", item.get("action")),
                ("处理位置", item.get("location")),
            ]
            text_lines.append(f"\n{index}. {_clean(item.get('context'))}")
            text_lines.extend(f"{label}: {_clean(value)}" for label, value in rows[1:])
            cards.append(
                '<div style="margin:12px 0;padding:14px;border:1px solid #fecaca;background:#fef2f2;border-radius:10px">'
                + _html_table(rows)
                + "</div>"
            )
        html_sections.append(
            '<div style="margin-top:22px"><h3>需要处理</h3>'
            + "".join(cards)
            + "</div>"
        )

    maintenance = list(report.get("maintenance_items", []))
    if maintenance:
        text_lines += ["", "非紧急维护（与发布分开）"]
        cards = []
        for item in maintenance:
            rows = [
                ("说明", item.get("context")),
                ("影响", item.get("impact")),
                ("以后怎么做", item.get("action")),
                ("位置", item.get("location")),
            ]
            text_lines.append(f"- {_clean(item.get('context'))}")
            text_lines.append(f"  {_clean(item.get('action'))}")
            cards.append(
                '<div style="margin:10px 0;padding:12px;border:1px solid #d4d4d8;background:#fafafa;border-radius:8px">'
                + _html_table(rows)
                + "</div>"
            )
        html_sections.append(
            '<div style="margin-top:20px;color:#52525b"><h3>非紧急维护（与发布分开）</h3>'
            + "".join(cards)
            + "</div>"
        )


def build_message(report: dict[str, Any]) -> dict[str, str]:
    """Render owner-facing mail without machine-only blocker codes."""
    subject, headline = _subject_and_headline(report)
    status = str(report.get("status") or "unknown")
    rows = [
        ("目标节目", report.get("show_name") or report.get("show_id")),
        ("公开受众", report.get("audience")),
        ("开始", report.get("started_at")),
        ("结束", report.get("finished_at")),
    ]
    if status == "failed":
        rows.extend(
            [
                ("错误类型", report.get("error_type")),
                ("错误", report.get("error")),
                ("日志", report.get("log_path")),
            ]
        )
    else:
        rows.extend(
            [
                ("计划 hash", report.get("plan_hash")),
                ("发布 scope hash", report.get("publish_scope_hash")),
                ("执行计划", report.get("plan_path")),
            ]
        )
    text_lines = [headline, *[f"{label}: {_clean(value)}" for label, value in rows]]
    html_sections = [_html_table(rows)]

    if status == "completed":
        for item in report.get("publish_items", []):
            title = str(item.get("title") or item.get("video_id") or "未命名单集")
            item_rows = [
                ("集数/标题", title),
                ("Episode ID", item.get("episode_id")),
                ("发布日期", item.get("published_at")),
                ("Transistor", item.get("transistor_result") or "published + readback verified"),
                ("YouTube", item.get("youtube_url")),
                (
                    "Show Notes",
                    f"{item.get('description_source') or 'unknown'} / {item.get('description_format') or 'plain'} / {item.get('description_chars') or 0} chars",
                ),
                (
                    "文字稿",
                    f"{item.get('transcript_source_status') or 'missing'} / {item.get('transcript_chars') or 0} chars",
                ),
            ]
            text_lines += ["", f"已发布：{title}"]
            text_lines.extend(f"{label}: {_clean(value)}" for label, value in item_rows)
            html_sections.append(
                '<div style="margin-top:22px;padding:14px;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px">'
                + _html_table(item_rows)
                + "</div>"
            )

        platform_rows = [
            ("Transistor API", (report.get("platform_results") or {}).get("transistor")),
            ("公开 RSS", (report.get("platform_results") or {}).get("rss")),
            ("小宇宙等下游", (report.get("platform_results") or {}).get("downstream")),
            ("执行 ledger", report.get("ledger_path")),
            ("逐集质检", (report.get("post_publish_checks") or {}).get("quality")),
            ("重排检查", (report.get("post_publish_checks") or {}).get("reorder")),
            ("重建计划", (report.get("post_publish_checks") or {}).get("rebuild")),
        ]
        text_lines += ["", "平台与质检结果"]
        text_lines.extend(f"{label}: {_clean(value)}" for label, value in platform_rows)
        html_sections.append(
            '<div style="margin-top:22px"><h3>平台与质检结果</h3>'
            + _html_table(platform_rows)
            + "</div>"
        )
        rollback = (
            "如需撤回，可在 Transistor 将该 episode 改回 Draft；"
            "但已被 RSS 下游抓取或缓存的副本不保证同步消失。"
        )
        text_lines += ["", rollback]
        html_sections.append(
            f'<p style="margin-top:18px;color:#52525b">{html.escape(rollback)}</p>'
        )

    partial = list(report.get("partial_publications", []))
    if partial:
        text_lines += ["", "已确认的部分发布（不要重复创建）"]
        partial_rows: list[tuple[str, Any]] = []
        for item in partial:
            label = str(item.get("video_id") or item.get("episode_id") or "episode")
            value = f"episode_id={item.get('episode_id')}, published_at={item.get('published_at')}"
            text_lines.append(f"- {label}: {value}")
            partial_rows.append((label, value))
        html_sections.append(
            '<div style="margin-top:20px"><h3>已确认的部分发布（不要重复创建）</h3>'
            + _html_table(partial_rows)
            + "</div>"
        )

    warnings = list(report.get("receipt_warnings", []))
    if warnings and status == "completed":
        text_lines += ["", "非阻塞质量回执"]
        text_lines.extend(f"- {_clean(item.get('detail'))}" for item in warnings)
        html_sections.append(
            '<div style="margin-top:18px;color:#52525b"><h3>非阻塞质量回执</h3><ul>'
            + "".join(
                f"<li>{html.escape(_clean(item.get('detail')))}</li>" for item in warnings
            )
            + "</ul></div>"
        )

    _append_action_sections(report, text_lines, html_sections)
    if report.get("feed_url"):
        text_lines += ["", f"公开 RSS: {report['feed_url']}"]
        html_sections.append(
            f'<p style="margin-top:16px"><a href="{html.escape(str(report["feed_url"]), quote=True)}">查看公开 RSS</a></p>'
        )

    html_body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;max-width:720px;color:#18181b">'
        f'<h2 style="margin:0 0 16px">{html.escape(headline)}</h2>'
        + "".join(html_sections)
        + "</div>"
    )
    return {"subject": subject, "text": "\n".join(text_lines), "html": html_body}


def send_report(
    report: dict[str, Any],
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Send one idempotent report; configuration/delivery errors are non-fatal."""
    config, missing = EmailConfig.from_env()
    if config is None:
        return {
            "status": "skipped",
            "reason": "not_configured" if not missing else "incomplete_configuration",
            "missing": missing,
        }

    message = build_message(report)
    identity = "/".join(
        [
            str(report.get("started_at") or "unknown"),
            str(report.get("status") or "unknown"),
            str(report.get("plan_hash") or report.get("error_type") or "unknown")[:64],
        ]
    )
    payload = {
        "from": config.sender,
        "to": list(config.recipients),
        "reply_to": REPLY_TO,
        **message,
        "tags": [
            {"name": "workflow", "value": "kedaibiao-podcast-sync"},
            {"name": "status", "value": str(report.get("status") or "unknown")[:256]},
        ],
    }
    client = session or requests.Session()
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"kedaibiao-podcast-sync/{identity}"[:256],
    }
    last_error = "unknown"
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            response = client.post(
                RESEND_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            retryable = True
        else:
            if response.status_code in (200, 201):
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                return {
                    "status": "sent",
                    "email_id": body.get("id"),
                    "recipient_count": len(config.recipients),
                }
            last_error = (
                f"HTTP {response.status_code}: "
                f"{response.text[:300].replace(chr(10), ' ')}"
            )
            retryable = response.status_code == 429 or response.status_code >= 500
        if not retryable or attempt == max_attempts:
            break
        sleep(min(8.0, 2 ** (attempt - 1)))
    return {"status": "failed", "error": last_error, "attempts": attempts}
