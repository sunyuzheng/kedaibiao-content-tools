#!/usr/bin/env python3
"""Pure validation helpers for podcast-specific show-notes sidecars."""

from __future__ import annotations

import html
import re
from typing import Any


MAX_SHOW_NOTES_CHARS = 10_000
MIN_RECOMMENDED_CHARS = 300
LEGACY_LINKS = (
    "staysuperlinear.com",
    "www.superlinear.academy/ai-builders",
    "superlinear.academy/c/share-your-projects",
)
ALLOWED_PLACEHOLDERS = {
    "campaign_end",
    "campaign_start",
    "chapters",
    "donate",
    "new_supporters",
    "people",
    "supporters",
    "transcript",
    "video",
}
PLACEHOLDER_TOKEN_RE = re.compile(r"{{.*?}}", re.DOTALL)
PLACEHOLDER_FULL_RE = re.compile(
    r"{{\s*(?P<name>[a-z_]+)"
    r"(?:\s*\|\s*title\s*:\s*(?P<quote>['\"])"
    r"(?P<title>[^{}'\"]*)(?P=quote))?\s*}}"
)
BRACE_MARKER_RE = re.compile(r"{{|}}")
TIMESTAMP_RE = re.compile(
    r"^(?P<stamp>(?:\d{1,2}:)?\d{1,3}:\d{2})\s+(?:—|-)\s+\S",
    re.MULTILINE,
)
TIMESTAMP_LINE_RE = re.compile(
    r"^(?:\d{1,2}:)?\d{1,3}:\d{2}\s+(?:—|-)\s+\S"
)
FULL_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
SIMPLE_HTML_RE = re.compile(
    r"</?(?:a|br|div|h[1-6]|li|ol|p|span|strong|ul)\b",
    re.IGNORECASE,
)
PORTABLE_HTML_FORMAT = "portable_html_v1"
PORTABLE_HTML_HEADINGS = {
    "这期你会听到",
    "章节",
    "本期嘉宾",
    "本期人物",
    "相关内容",
    "本期相关",
    "加入Superlinear Academy免费社区",
    "关于主播与节目",
}


def timestamp_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        if seconds >= 60:
            raise ValueError(f"Invalid timestamp: {value}")
        return minutes * 60 + seconds
    if len(parts) != 3:
        raise ValueError(f"Invalid timestamp: {value}")
    hours, minutes, seconds = parts
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Invalid timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds


def placeholder_tokens(value: str) -> tuple[list[str], list[str]]:
    """Return exact supported token names and deterministic syntax errors."""
    errors: list[str] = []
    depth = 0
    for marker in BRACE_MARKER_RE.finditer(value):
        if marker.group() == "{{":
            if depth:
                errors.append("nested_placeholder_braces")
            depth += 1
        else:
            if depth != 1:
                errors.append("unbalanced_placeholder_braces")
                depth = max(0, depth - 1)
            else:
                depth = 0
    if depth or value.count("{{") != value.count("}}"):
        errors.append("unbalanced_placeholder_braces")

    names: list[str] = []
    for token in PLACEHOLDER_TOKEN_RE.findall(value):
        match = PLACEHOLDER_FULL_RE.fullmatch(token)
        if not match:
            errors.append("malformed_placeholder")
            continue
        name = match.group("name")
        names.append(name)
        if name not in ALLOWED_PLACEHOLDERS:
            errors.append(f"unsupported_placeholder:{name}")
        title = match.group("title") or ""
        if any(character in title for character in "<>&"):
            errors.append("unsafe_placeholder_title")
    return names, sorted(set(errors))


def _portable_inline(value: str) -> str:
    """Escape one plain-text line and linkify a full-line URL."""
    clean = value.strip()
    if FULL_URL_RE.fullmatch(clean):
        escaped_url = html.escape(clean, quote=True)
        return (
            f'<a href="{escaped_url}">'
            f"{html.escape(clean, quote=False)}</a>"
        )
    return html.escape(clean, quote=False)


def _dynamic_link_block(lines: list[str]) -> str | None:
    """Collapse a redundant label + Transistor dynamic-link tag to one link."""
    if not lines:
        return None
    match = PLACEHOLDER_FULL_RE.fullmatch(lines[-1].strip())
    if not match or match.group("name") not in {"video", "transcript"}:
        return None
    title = (match.group("title") or "").strip()
    if any(character in title for character in "<>&"):
        raise ValueError("portable HTML renderer rejected unsafe placeholder title")
    labels = [line.strip().rstrip(":：") for line in lines[:-1] if line.strip()]
    if labels and (not title or any(label != title for label in labels)):
        return None
    return f"<p>{lines[-1].strip()}</p>"


def render_portable_show_notes_html(text: str) -> str:
    """Render canonical plain-text Show Notes into conservative feed HTML.

    Podcast apps do not reliably preserve raw newlines from RSS descriptions.
    This renderer uses only simple paragraphs, strong section labels, literal
    bullet glyphs, hyperlinks, and Transistor's supported dynamic tags. The
    canonical sidecar stays easy to review while the exact public payload is
    deterministic and approval-hashable.
    """
    value = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return ""
    if SIMPLE_HTML_RE.search(value):
        raise ValueError("portable HTML renderer requires plain-text Show Notes")

    rendered_blocks: list[str] = []
    for block in re.split(r"\n\s*\n+", value):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        dynamic = _dynamic_link_block(lines)
        if dynamic is not None:
            rendered_blocks.append(dynamic)
            continue

        if len(lines) == 1 and lines[0] in PORTABLE_HTML_HEADINGS:
            rendered_blocks.append(
                f"<p><strong>{html.escape(lines[0], quote=False)}</strong></p>"
            )
            continue

        rendered_lines: list[str] = []
        for line in lines:
            placeholder = PLACEHOLDER_FULL_RE.fullmatch(line)
            if placeholder:
                if placeholder.group("name") not in ALLOWED_PLACEHOLDERS:
                    raise ValueError(
                        "portable HTML renderer rejected unsupported placeholder"
                    )
                title = placeholder.group("title") or ""
                if any(character in title for character in "<>&"):
                    raise ValueError(
                        "portable HTML renderer rejected unsafe placeholder title"
                    )
                rendered_lines.append(f"<p>{line}</p>")
            elif "{{" in line or "}}" in line:
                raise ValueError(
                    "portable HTML renderer rejected malformed placeholder line"
                )
            elif line.startswith("- "):
                rendered_lines.append(
                    f"<p>• {_portable_inline(line[2:])}</p>"
                )
            elif TIMESTAMP_LINE_RE.match(line):
                # Keep the timestamp at the beginning of a physical source
                # line so the existing duration/order validator still sees it
                # in the immutable rendered payload.
                rendered_lines.append(
                    f"<p>\n{_portable_inline(line)}\n</p>"
                )
            else:
                rendered_lines.append(f"<p>{_portable_inline(line)}</p>")
        rendered_blocks.append("\n".join(rendered_lines))

    return "\n\n".join(rendered_blocks)


def validate_show_notes(text: str) -> dict[str, Any]:
    """Return deterministic hard errors, warnings, and basic quality metadata."""
    value = text.strip()
    errors: list[str] = []
    warnings: list[str] = []

    if not value:
        errors.append("empty")
    if len(value) > MAX_SHOW_NOTES_CHARS:
        errors.append("over_10000_chars")
    placeholders, placeholder_errors = placeholder_tokens(value)
    errors.extend(placeholder_errors)

    timestamps: list[int] = []
    for match in TIMESTAMP_RE.finditer(value):
        stamp = match.group("stamp")
        try:
            timestamps.append(timestamp_seconds(stamp))
        except ValueError:
            errors.append(f"invalid_timestamp:{stamp}")
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        errors.append("timestamps_not_strictly_increasing")

    if value and len(value) < MIN_RECOMMENDED_CHARS:
        warnings.append("under_300_chars")
    for legacy in LEGACY_LINKS:
        if legacy.lower() in value.lower():
            warnings.append(f"legacy_link:{legacy}")
    if value and "https://www.superlinear.academy/" not in value:
        warnings.append("missing_canonical_community_url")

    return {
        "chars": len(value),
        "errors": errors,
        "warnings": warnings,
        "timestamps": len(timestamps),
        "last_timestamp_seconds": timestamps[-1] if timestamps else None,
        "placeholders": placeholders,
    }
