"""Stable, independently numbered podcast series; no network or remote writes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .core import PROJECT_ROOT, strip_episode_number

CATALOG_PATH = PROJECT_ROOT / "podcast_series.json"
SERIES_LABELS = {"dialogue": "对话", "solo": "立正说"}
SERIES_PREFIX_RE = re.compile(r"^(对话|立正说) (\d{3,})｜(.+)$")


def validate_catalog(entries: dict[str, dict[str, Any]]) -> None:
    slots: set[tuple[str, int]] = set()
    global_numbers: set[int] = set()
    for video_id, entry in entries.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError(f"Invalid series video_id: {video_id}")
        series, number = entry.get("series"), entry.get("series_number")
        if series not in SERIES_LABELS or type(number) is not int or number < 1:
            raise ValueError(f"Invalid series assignment: {video_id}")
        if (series, number) in slots:
            raise ValueError(f"Duplicate series number: {series} {number}")
        slots.add((series, number))
        global_number = entry.get("global_number")
        if global_number is not None:
            if type(global_number) is not int or global_number < 1 or global_number in global_numbers:
                raise ValueError(f"Invalid/duplicate global number: {video_id}")
            global_numbers.add(global_number)
        if not entry.get("classification_basis"):
            raise ValueError(f"Missing classification evidence: {video_id}")


def load_catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    data = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("episodes"), dict):
        raise ValueError("Unsupported podcast series catalog")
    entries = data["episodes"]
    validate_catalog(entries)
    return entries


def get_assignment(video_id: str, *, catalog: dict | None = None) -> dict:
    entries = load_catalog() if catalog is None else catalog
    if video_id not in entries:
        raise ValueError(f"podcast_series_unclassified:{video_id}")
    return entries[video_id]


def episode_series_title(title: str, video_id: str, *, catalog: dict | None = None) -> str:
    assignment = get_assignment(video_id, catalog=catalog)
    body = strip_episode_number(title)
    if not body:
        raise ValueError(f"Empty podcast title: {video_id}")
    return f"{SERIES_LABELS[assignment['series']]} {assignment['series_number']:03d}｜{body}"


def next_assignment(entries: dict, series: str, date: str, basis: str) -> dict:
    """Append a reviewed assignment. Never renumber existing episodes/backfills."""
    if series not in SERIES_LABELS or not re.fullmatch(r"\d{8}", date) or not basis.strip():
        raise ValueError("Series, YYYYMMDD date and classification evidence are required")
    number = 1 + max((e["series_number"] for e in entries.values() if e["series"] == series), default=0)
    return {"series": series, "series_number": number, "source_date": date,
            "classification_basis": basis.strip()}
