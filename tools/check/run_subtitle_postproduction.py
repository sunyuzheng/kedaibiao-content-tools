#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run subtitle post-production from the canonical library manifest.

This script handles only subtitle assets:
- action_needed=transcribe_then_review: local Qwen ASR -> Codex correction -> final SRT
- action_needed=review_subtitle_before_publish: existing timed subtitle -> Codex correction -> final SRT

It intentionally skips highlights, articles, titles, and YouTube descriptions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_ROOT = Path(
    os.environ.get(
        "KEDAIBIAO_SUBTITLE_PIPELINE_ROOT",
        str(PROJECT_ROOT.parent / "lizheng-video-editing"),
    )
).expanduser()
PIPELINE_PYTHON = PIPELINE_ROOT / "venv" / "bin" / "python"
PROCESS_VIDEO = PIPELINE_ROOT / "tools" / "process_video.py"
MANIFEST = PROJECT_ROOT / "logs" / "library_manifest" / "library_manifest.json"
RUN_ROOT = PROJECT_ROOT / "logs" / "subtitle_postproduction"


def load_manifest() -> list[dict[str, Any]]:
    if not MANIFEST.exists():
        raise SystemExit(f"Manifest not found: {MANIFEST}. Run tools/check/build_library_manifest.py first.")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["records"]


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def episode_stem(media_path: Path) -> str:
    return media_path.with_suffix("").name


def process_dir_for(media_path: Path) -> Path:
    return media_path.parent / f"{episode_stem(media_path)}_process"


def first_media(folder: Path, rec: dict[str, Any]) -> Path | None:
    for name in rec.get("audio_files") or []:
        p = folder / name
        if p.exists():
            return p
    for pattern in ("*.m4a", "*.mp3", "*.wav", "*.aac", "*.opus", "*.mp4", "*.mov", "*.webm"):
        found = sorted(folder.glob(pattern))
        if found:
            return found[0]
    return None


def media_duration_seconds(media: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(media),
            ],
            text=True,
            timeout=15,
        ).strip()
        return float(out)
    except Exception:
        return 0.0


def parse_vtt_timestamp(ts: str) -> str:
    ts = ts.strip().replace(".", ",")
    if ts.count(":") == 1:
        ts = "00:" + ts
    if "," not in ts:
        ts += ",000"
    return ts


def vtt_to_srt(src: Path, dst: Path) -> None:
    lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    cues: list[tuple[str, str, list[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if (
            not line
            or line == "WEBVTT"
            or line.startswith(("NOTE", "STYLE", "REGION"))
        ):
            i += 1
            continue
        if "-->" not in line and i + 1 < len(lines) and "-->" in lines[i + 1]:
            i += 1
            line = lines[i].strip()
        if "-->" not in line:
            i += 1
            continue
        start, end = [part.strip().split(" ")[0] for part in line.split("-->", 1)]
        i += 1
        text: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = re.sub(r"<[^>]+>", "", lines[i]).strip()
            if cleaned:
                text.append(cleaned)
            i += 1
        if text:
            cues.append((parse_vtt_timestamp(start), parse_vtt_timestamp(end), text))
    with dst.open("w", encoding="utf-8") as f:
        for idx, (start, end, text) in enumerate(cues, 1):
            f.write(f"{idx}\n{start} --> {end}\n")
            f.write("\n".join(text))
            f.write("\n\n")


def normalize_srt(src: Path, dst: Path) -> None:
    text = src.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    dst.write_text(text.strip() + "\n", encoding="utf-8")


def best_existing_subtitle(folder: Path, rec: dict[str, Any], stem: str) -> tuple[Path, str] | None:
    process_dir = folder / f"{stem}_process"
    candidates = [
        process_dir / f"{stem}.qwen.srt",
        folder / f"{stem}.qwen.srt",
        folder / f"{stem}.zh-Hans.srt",
        folder / f"{stem}.zh.srt",
        folder / f"{stem}.zh-Hant.srt",
        folder / f"{stem}.srt",
        folder / f"{stem}.zh-Hans.vtt",
        folder / f"{stem}.zh.vtt",
        folder / f"{stem}.zh-Hant.vtt",
        folder / f"{stem}.vtt",
    ]
    for p in candidates:
        if p.exists():
            source_type = "local_qwen" if p.name.endswith(".qwen.srt") else "downloaded_or_legacy_subtitle"
            return p, source_type
    for name in rec.get("subtitle_files") or []:
        p = folder / name
        if p.exists() and p.suffix.lower() in {".srt", ".vtt"}:
            return p, "downloaded_or_legacy_subtitle"
    return None


def prepare_existing_subtitle(media: Path, rec: dict[str, Any]) -> dict[str, Any]:
    stem = episode_stem(media)
    folder = media.parent
    process_dir = process_dir_for(media)
    process_dir.mkdir(parents=True, exist_ok=True)
    qwen_path = process_dir / f"{stem}.qwen.srt"

    found = best_existing_subtitle(folder, rec, stem)
    if not found:
        raise RuntimeError("No existing subtitle file found for review.")
    src, source_type = found
    if src.stat().st_size <= 20:
        raise RuntimeError(f"Existing subtitle is empty or too small: {rel(src)}")

    if src.resolve() != qwen_path.resolve():
        if src.suffix.lower() == ".vtt":
            vtt_to_srt(src, qwen_path)
        else:
            normalize_srt(src, qwen_path)

    return {
        "prepared_input": rel(qwen_path),
        "source_file": rel(src),
        "source_type": source_type,
    }


def run_process_video(media: Path, skip_transcribe: bool, dry_run: bool) -> dict[str, Any]:
    cmd = [
        str(PIPELINE_PYTHON),
        str(PROCESS_VIDEO),
        str(media),
        "--no-seeds",
        "--skip-highlights",
        "--skip-article",
        "--skip-titles",
        "--skip-youtube-description",
    ]
    if skip_transcribe:
        cmd.append("--skip-transcribe")

    started = time.time()
    if dry_run:
        return {"cmd": cmd, "returncode": None, "elapsed_seconds": 0}
    proc = subprocess.run(
        cmd,
        cwd=str(PIPELINE_ROOT),
        text=True,
        capture_output=True,
    )
    elapsed = int(time.time() - started)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def final_paths(media: Path) -> dict[str, str | bool]:
    stem = episode_stem(media)
    process_dir = process_dir_for(media)
    return {
        "qwen_srt": rel(process_dir / f"{stem}.qwen.srt"),
        "corrected_srt": rel(process_dir / f"{stem}.corrected.srt"),
        "final_srt": rel(media.parent / f"{stem}.final.srt"),
        "has_qwen_srt": (process_dir / f"{stem}.qwen.srt").exists(),
        "has_corrected_srt": (process_dir / f"{stem}.corrected.srt").exists(),
        "has_final_srt": (media.parent / f"{stem}.final.srt").exists(),
    }


def cleanup_empty_working_subtitles(media: Path) -> None:
    stem = episode_stem(media)
    process_dir = process_dir_for(media)
    for p in [
        process_dir / f"{stem}.qwen.srt",
        process_dir / f"{stem}.corrected.srt",
        media.parent / f"{stem}.final.srt",
    ]:
        if p.exists() and p.stat().st_size <= 20:
            p.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch subtitle transcription/correction from manifest.")
    parser.add_argument(
        "--actions",
        nargs="+",
        default=["transcribe_then_review", "review_subtitle_before_publish"],
        help="Manifest action_needed values to process.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit number of records processed.")
    parser.add_argument("--max-duration-minutes", type=float, default=0, help="Only process media at or below this duration.")
    parser.add_argument("--min-duration-minutes", type=float, default=0, help="Only process media at or above this duration.")
    parser.add_argument(
        "--sort",
        choices=["date", "duration", "duration-desc"],
        default="date",
        help="Processing order.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--video-id", action="append", default=[], help="Only process specific video id(s).")
    args = parser.parse_args()

    if not PIPELINE_PYTHON.exists() or not PROCESS_VIDEO.exists():
        raise SystemExit(f"KDB pipeline not available at {PIPELINE_ROOT}")

    records = load_manifest()
    selected: list[dict[str, Any]] = []
    for r in records:
        if not (
            r.get("action_needed") in set(args.actions)
            and (not args.video_id or r.get("video_id") in set(args.video_id))
        ):
            continue
        folder = PROJECT_ROOT / r["folder"]
        media = first_media(folder, r)
        duration = media_duration_seconds(media) if media else 0.0
        if args.max_duration_minutes and duration > args.max_duration_minutes * 60:
            continue
        if args.min_duration_minutes and duration < args.min_duration_minutes * 60:
            continue
        item = dict(r)
        item["_media_duration_seconds"] = duration
        selected.append(item)
    if args.sort == "duration":
        selected.sort(key=lambda r: (r.get("_media_duration_seconds") or 0, r.get("upload_date") or "", r.get("video_id") or ""))
    elif args.sort == "duration-desc":
        selected.sort(key=lambda r: (-(r.get("_media_duration_seconds") or 0), r.get("upload_date") or "", r.get("video_id") or ""))
    else:
        selected.sort(key=lambda r: (r.get("upload_date") or "", r.get("video_id") or ""))
    if args.limit:
        selected = selected[:args.limit]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "run_state.jsonl"
    summary = {
        "run_id": run_id,
        "dry_run": args.dry_run,
        "selected": len(selected),
        "actions": args.actions,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": rel(run_dir),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    for idx, rec in enumerate(selected, 1):
        folder = PROJECT_ROOT / rec["folder"]
        media = first_media(folder, rec)
        event: dict[str, Any] = {
            "index": idx,
            "total": len(selected),
            "video_id": rec.get("video_id"),
            "title": rec.get("title"),
            "action_needed": rec.get("action_needed"),
            "transcript_status_before": rec.get("transcript_status"),
            "media_duration_seconds": rec.get("_media_duration_seconds"),
            "folder": rec.get("folder"),
            "youtube_url": f"https://www.youtube.com/watch?v={rec.get('video_id')}" if rec.get("video_id") else "",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            if not media:
                raise RuntimeError("No local media file found.")
            event["media_file"] = rel(media)
            cleanup_empty_working_subtitles(media)
            if rec.get("action_needed") == "review_subtitle_before_publish":
                event.update(prepare_existing_subtitle(media, rec))
                skip_transcribe = True
            else:
                event["source_type"] = "local_qwen_asr"
                skip_transcribe = False
            print(f"[{idx}/{len(selected)}] {event['action_needed']} {event['video_id']} {event['title']}", flush=True)
            event["process"] = run_process_video(media, skip_transcribe=skip_transcribe, dry_run=args.dry_run)
            event.update(final_paths(media))
            produced_final = bool(event.get("has_corrected_srt") and event.get("has_final_srt"))
            event["status"] = (
                "ok" if args.dry_run or (event["process"]["returncode"] == 0 and produced_final)
                else "failed"
            )
            if event["status"] == "failed" and event["process"].get("returncode") == 0 and not produced_final:
                event["error"] = "Process exited 0 but corrected/final SRT was not produced."
        except Exception as exc:
            event["status"] = "failed"
            event["error"] = str(exc)
        event["finished_at"] = datetime.now().isoformat(timespec="seconds")
        with state_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(f"  -> {event['status']}", flush=True)
        if event["status"] == "failed" and not args.dry_run:
            print(json.dumps(event, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)

    print(f"State: {state_path}")


if __name__ == "__main__":
    main()
