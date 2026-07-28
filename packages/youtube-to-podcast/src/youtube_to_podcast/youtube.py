"""yt-dlp boundary for public-channel discovery and local media preparation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError
from .hashing import sha256_file, sha256_text
from .transcript import choose_transcript, timed_text_to_text


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class ListedVideo:
    video_id: str
    title: str
    playlist_index: int


Runner = Callable[..., subprocess.CompletedProcess[str]]


def ytdlp_binary() -> str:
    sibling = Path(sys.executable).with_name("yt-dlp")
    if sibling.is_file():
        return str(sibling)
    binary = shutil.which("yt-dlp")
    if not binary:
        raise ConfigError("yt-dlp is not installed or not on PATH")
    return binary


def _run_json(command: list[str], *, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = runner(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        last_line = detail[-1][-1000:] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"yt-dlp failed: {last_line}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid JSON") from exc


def list_public_videos(
    channel_url: str,
    *,
    binary: str | None = None,
    runner: Runner = subprocess.run,
) -> list[ListedVideo]:
    payload = _run_json(
        [
            binary or ytdlp_binary(),
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            channel_url,
        ],
        runner=runner,
    )
    entries = payload.get("entries") or []
    videos: list[ListedVideo] = []
    for fallback_index, item in enumerate(entries, 1):
        video_id = str(item.get("id") or "")
        if not VIDEO_ID_RE.fullmatch(video_id):
            continue
        playlist_index = int(item.get("playlist_index") or fallback_index)
        videos.append(
            ListedVideo(
                video_id=video_id,
                title=str(item.get("title") or video_id),
                playlist_index=playlist_index,
            )
        )
    ids = [item.video_id for item in videos]
    indices = [item.playlist_index for item in videos]
    if not videos:
        raise RuntimeError("YouTube public listing returned zero usable videos")
    if len(ids) != len(set(ids)):
        raise RuntimeError("YouTube public listing returned duplicate video IDs")
    if len(indices) != len(set(indices)):
        raise RuntimeError("YouTube public listing returned duplicate positions")
    return videos


def fetch_metadata(
    video_id: str,
    *,
    binary: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    payload = _run_json(
        [
            binary or ytdlp_binary(),
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            "--no-playlist",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        runner=runner,
    )
    observed_id = str(payload.get("id") or "")
    if observed_id != video_id:
        raise RuntimeError(
            f"YouTube metadata mismatch: expected={video_id} observed={observed_id}"
        )
    thumbnails = payload.get("thumbnails") or []
    thumbnail = str(payload.get("thumbnail") or "")
    if not thumbnail and thumbnails:
        thumbnail = str(thumbnails[-1].get("url") or "")
    return {
        "video_id": video_id,
        "title": str(payload.get("title") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "upload_date": str(payload.get("upload_date") or ""),
        "duration": payload.get("duration"),
        "availability": payload.get("availability"),
        "is_live": bool(payload.get("is_live")),
        "was_live": bool(payload.get("was_live")),
        "live_status": payload.get("live_status"),
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "image_url": thumbnail
        or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def prepare_media(
    video_id: str,
    output_dir: Path,
    *,
    download_subtitles: bool,
    subtitle_languages: tuple[str, ...],
    binary: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "%(id)s.%(ext)s")
    command = [
        binary or ytdlp_binary(),
        "--no-playlist",
        "--no-warnings",
        "--continue",
        "--no-overwrites",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--write-info-json",
        "--output",
        template,
    ]
    if download_subtitles:
        command += [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            ",".join(subtitle_languages),
            "--sub-format",
            "srt/vtt/best",
        ]
    command.append(f"https://www.youtube.com/watch?v={video_id}")
    result = runner(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=7200,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        last_line = detail[-1][-1000:] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"yt-dlp media preparation failed: {last_line}")

    audio = output_dir / f"{video_id}.mp3"
    if not audio.is_file() or audio.stat().st_size == 0:
        raise RuntimeError(f"Prepared audio is missing: {audio}")
    subtitle_paths = sorted(
        [
            *output_dir.glob(f"{video_id}*.srt"),
            *output_dir.glob(f"{video_id}*.vtt"),
        ]
    )
    transcript_path = choose_transcript(subtitle_paths)
    transcript = timed_text_to_text(transcript_path) if transcript_path else ""
    return {
        "audio_path": audio,
        "audio_sha256": sha256_file(audio),
        "audio_bytes": audio.stat().st_size,
        "transcript_path": transcript_path,
        "transcript_sha256": sha256_text(transcript) if transcript else None,
        "transcript_chars": len(transcript),
    }
