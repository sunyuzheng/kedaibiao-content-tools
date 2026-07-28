#!/usr/bin/env python3
"""Pure, side-effect-free helpers shared by podcast planning and execution."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = PROJECT_ROOT / "archive"
YOUTUBE_SNAPSHOT = PROJECT_ROOT / "tools" / "youtube" / "all_videos_full.json"
TRANSISTOR_API_BASE = "https://api.transistor.fm/v1"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TITLE_NUMBER_RE = re.compile(r"^E\d+\.\s+")
TIMESTAMP_RE = re.compile(
    r"^(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+"
    r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
)


def load_env(path: Path | None = None) -> None:
    """Load a simple KEY=VALUE file without overwriting the process environment."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_value = value.strip()
        if (
            len(clean_value) >= 2
            and clean_value[0] == clean_value[-1]
            and clean_value[0] in {"'", '"'}
        ):
            clean_value = clean_value[1:-1]
        os.environ.setdefault(key.strip(), clean_value)


def require_transistor_config() -> tuple[str, str]:
    api_key = os.environ.get("TRANSISTOR_API_KEY", "").strip()
    show_id = os.environ.get("TRANSISTOR_SHOW_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("TRANSISTOR_API_KEY", api_key),
            ("TRANSISTOR_SHOW_ID", show_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return api_key, show_id


def canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_hash(plan: dict[str, Any]) -> str:
    unsigned = dict(plan)
    unsigned.pop("plan_hash", None)
    return sha256_text(canonical_json(unsigned))


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_video_id(value: str | None) -> str | None:
    if not value:
        return None
    if VIDEO_ID_RE.fullmatch(value):
        return value
    if "v=" in value:
        candidate = value.split("v=", 1)[1].split("&", 1)[0].strip()
        return candidate if VIDEO_ID_RE.fullmatch(candidate) else None
    if "youtu.be/" in value:
        candidate = value.split("youtu.be/", 1)[1].split("?", 1)[0].strip("/")
        return candidate if VIDEO_ID_RE.fullmatch(candidate) else None
    candidate = value[-11:]
    return candidate if VIDEO_ID_RE.fullmatch(candidate) else None


def published_at_from_yyyymmdd(value: str) -> str | None:
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT00:00:00Z")


def strip_episode_number(title: str) -> str:
    return TITLE_NUMBER_RE.sub("", title or "", count=1).strip()


def numbered_title(title: str, number: int) -> str:
    return f"E{number}. {strip_episode_number(title)}"


def _clean_caption_line(line: str) -> str:
    line = re.sub(r"<[^>]+>", "", line)
    line = html.unescape(line)
    return re.sub(r"\s+", " ", line).strip()


def timed_text_to_text(path: Path) -> str:
    """Convert SRT/VTT cues to readable text while collapsing cue overlap."""
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    lines: list[str] = []
    in_metadata_block = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            in_metadata_block = False
            continue
        if line == "WEBVTT" or line.startswith(("Kind:", "Language:", "X-TIMESTAMP-MAP=")):
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            in_metadata_block = True
            continue
        if in_metadata_block:
            continue
        if line.isdigit() or "-->" in line or TIMESTAMP_RE.match(line):
            continue
        cleaned = _clean_caption_line(line)
        if not cleaned:
            continue
        if lines and cleaned == lines[-1]:
            continue
        lines.append(cleaned)
    return "\n".join(lines).strip()


def choose_transcript_path(folder: Path, record: dict[str, Any] | None = None) -> Path | None:
    """Choose the highest-confidence local timed transcript deterministically."""
    record = record or {}
    status = record.get("transcript_status")
    candidates: list[Path] = []
    candidates.extend(folder.glob("*.srt"))
    candidates.extend(folder.glob("*.vtt"))
    for process_dir in sorted(folder.glob("*_process")):
        if process_dir.is_dir():
            candidates.extend(process_dir.glob("*.srt"))
            candidates.extend(process_dir.glob("*.vtt"))
    usable = sorted(
        {path.resolve() for path in candidates if path.is_file() and path.stat().st_size > 20},
        key=lambda path: str(path),
    )
    if not usable:
        return None

    def score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        if name.endswith(".corrected.srt") or name.endswith(".final.srt"):
            priority = 0
        elif status == "human" and any(
            marker in name for marker in (".zh-hans.", ".zh-hant.", ".zh.", ".en.")
        ):
            priority = 1
        elif ".qwen." in name:
            priority = 4
        elif name.endswith(".srt"):
            priority = 2
        elif name.endswith(".vtt"):
            priority = 3
        else:
            priority = 5
        return priority, str(path)

    return min(usable, key=score)


def first_existing(folder: Path, patterns: Iterable[str]) -> Path | None:
    for pattern in patterns:
        for path in sorted(folder.glob(pattern)):
            if path.is_file() and path.stat().st_size > 0:
                return path
    return None


def relative_to_project(path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = (PROJECT_ROOT / value).resolve()
    path.relative_to(PROJECT_ROOT.resolve())
    return path
