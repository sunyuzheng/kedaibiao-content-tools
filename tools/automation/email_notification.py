#!/usr/bin/env python3
"""Best-effort Resend notifications for the scheduled podcast reconciliation."""

from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


RESEND_ENDPOINT = "https://api.resend.com/emails"


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


def build_message(report: dict[str, Any]) -> dict[str, str]:
    """Render the same compact report as plain text and conservative HTML."""
    status = str(report.get("status") or "unknown")
    if status == "failed":
        subject = f"[课代表播客] 同步失败：{report.get('error_type') or 'unknown error'}"
        headline = "本周同步失败，需要检查"
    elif status == "attention":
        subject = (
            "[课代表播客] 待审核："
            f"发布 {report.get('publish_count', 0)} / "
            f"字幕 {report.get('transcript_count', 0)} / "
            f"阻止 {report.get('blocked_count', 0)}"
        )
        headline = "已完成只读对账，有事项需要审核"
    else:
        subject = "[课代表播客] 本周同步正常，无待处理项"
        headline = "已完成本周只读对账"

    rows = [
        ("状态", status),
        ("开始时间", report.get("started_at") or "unknown"),
        ("结束时间", report.get("finished_at") or "unknown"),
    ]
    if status == "failed":
        rows.extend(
            [
                ("错误类型", report.get("error_type") or "unknown"),
                ("错误", report.get("error") or "unknown"),
                ("日志", report.get("log_path") or "unknown"),
            ]
        )
    else:
        rows.extend(
            [
                ("待发布", report.get("publish_count", 0)),
                ("待上传字幕", report.get("transcript_count", 0)),
                ("被阻止", report.get("blocked_count", 0)),
                (
                    "YouTube 证据新鲜",
                    "是" if report.get("youtube_snapshot_fresh") else "否",
                ),
                ("计划 hash", report.get("plan_hash") or "unknown"),
                ("审批计划", report.get("plan_path") or "unknown"),
            ]
        )

    text_lines = [headline, ""]
    html_rows: list[str] = []
    for label, value in rows:
        clean_value = str(value).replace("\r", " ").replace("\n", " ")[:1000]
        text_lines.append(f"{label}: {clean_value}")
        html_rows.append(
            "<tr>"
            f"<th style=\"text-align:left;padding:6px 12px 6px 0;color:#52525b\">"
            f"{html.escape(str(label))}</th>"
            f"<td style=\"padding:6px 0;color:#18181b\">"
            f"{html.escape(clean_value)}</td>"
            "</tr>"
        )
    text_lines += [
        "",
        "这是只读计划通知；定时任务不会自动发布或修改 Transistor。",
    ]
    html_body = (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:680px;color:#18181b\">"
        f"<h2 style=\"margin:0 0 16px\">{html.escape(headline)}</h2>"
        "<table style=\"border-collapse:collapse\">"
        + "".join(html_rows)
        + "</table>"
        "<p style=\"margin-top:20px;color:#52525b\">"
        "这是只读计划通知；定时任务不会自动发布或修改 Transistor。"
        "</p></div>"
    )
    return {
        "subject": subject,
        "text": "\n".join(text_lines),
        "html": html_body,
    }


def send_report(
    report: dict[str, Any],
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Send one idempotent report; configuration and delivery errors are non-fatal."""
    config, missing = EmailConfig.from_env()
    if config is None:
        return {
            "status": "skipped",
            "reason": "not_configured" if not missing else "incomplete_configuration",
            "missing": missing,
        }

    message = build_message(report)
    started_at = str(report.get("started_at") or "unknown")
    idempotency_key = f"kedaibiao-podcast-sync/{started_at}"[:256]
    payload = {
        "from": config.sender,
        "to": list(config.recipients),
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
        "Idempotency-Key": idempotency_key,
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
    return {
        "status": "failed",
        "error": last_error,
        "attempts": attempts,
    }
