"""Conservative SRT/VTT-to-plain-text conversion for Transistor."""

from __future__ import annotations

import html
import re
from pathlib import Path


TIMESTAMP_RE = re.compile(
    r"^(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+"
    r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
)
TAG_RE = re.compile(r"<[^>]+>")


def timed_text_to_text(path: Path) -> str:
    lines: list[str] = []
    in_metadata_block = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            in_metadata_block = False
            continue
        if line == "WEBVTT" or line.startswith(
            ("Kind:", "Language:", "X-TIMESTAMP-MAP=")
        ):
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            in_metadata_block = True
            continue
        if in_metadata_block:
            continue
        if line.isdigit() or "-->" in line or TIMESTAMP_RE.match(line):
            continue
        cleaned = html.unescape(TAG_RE.sub("", line)).strip()
        if not cleaned or (lines and cleaned == lines[-1]):
            continue
        lines.append(cleaned)
    return "\n".join(lines).strip()


def choose_transcript(paths: list[Path]) -> Path | None:
    usable = [path for path in paths if path.is_file() and path.stat().st_size > 20]
    if not usable:
        return None

    def score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        if name.endswith(".srt"):
            rank = 0
        elif name.endswith(".vtt"):
            rank = 1
        else:
            rank = 2
        automatic_penalty = 1 if ".auto." in name else 0
        return rank + automatic_penalty, name

    return min(usable, key=score)
