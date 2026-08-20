#!/usr/bin/env python3
"""Pure validation helpers for podcast-specific show-notes sidecars."""

from __future__ import annotations

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
PLACEHOLDER_RE = re.compile(r"{{\s*([a-z_]+)(?:\s*\|[^{}]+)?\s*}}")
TIMESTAMP_RE = re.compile(
    r"^(?P<stamp>(?:\d{1,2}:)?\d{1,3}:\d{2})\s+(?:—|-)\s+\S",
    re.MULTILINE,
)


def timestamp_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def validate_show_notes(text: str) -> dict[str, Any]:
    """Return deterministic hard errors, warnings, and basic quality metadata."""
    value = text.strip()
    errors: list[str] = []
    warnings: list[str] = []

    if not value:
        errors.append("empty")
    if len(value) > MAX_SHOW_NOTES_CHARS:
        errors.append("over_10000_chars")
    if value.count("{{") != value.count("}}"):
        errors.append("unbalanced_placeholder_braces")

    placeholders = PLACEHOLDER_RE.findall(value)
    unsupported = sorted(set(placeholders) - ALLOWED_PLACEHOLDERS)
    if unsupported:
        errors.extend(f"unsupported_placeholder:{name}" for name in unsupported)

    timestamps = [
        timestamp_seconds(match.group("stamp"))
        for match in TIMESTAMP_RE.finditer(value)
    ]
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
