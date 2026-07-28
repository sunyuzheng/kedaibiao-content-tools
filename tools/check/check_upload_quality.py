#!/usr/bin/env python3
"""Read-only Transistor quality checks with local source readback validation."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.check.build_library_manifest import local_records  # noqa: E402
from tools.podcast.core import (  # noqa: E402
    choose_transcript_path,
    extract_video_id,
    load_env,
    published_at_from_yyyymmdd,
    require_transistor_config,
    sha256_text,
    timed_text_to_text,
)
from tools.podcast.transistor_client import TransistorClient  # noqa: E402


TITLE_RE = re.compile(r"^E(\d+)\.\s+.+")
YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[A-Za-z0-9_-]{11}"
)


def local_expectations() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in local_records():
        video_id = record.get("video_id")
        if not video_id:
            continue
        folder = PROJECT_ROOT / record["folder"]
        transcript_path = choose_transcript_path(folder, record)
        transcript = timed_text_to_text(transcript_path) if transcript_path else ""
        result[video_id] = {
            "published_at": published_at_from_yyyymmdd(record.get("upload_date") or ""),
            "transcript_path": str(transcript_path.relative_to(PROJECT_ROOT)) if transcript_path else None,
            "transcript_sha256": sha256_text(transcript) if transcript else None,
            "transcript_chars": len(transcript),
        }
    return result


def check_episode(
    episode: dict[str, Any],
    expected: dict[str, Any] | None,
    *,
    require_published: bool,
) -> list[str]:
    attrs = episode.get("attributes", {})
    title = attrs.get("title") or ""
    number = attrs.get("number")
    status = attrs.get("status") or ""
    published_at = attrs.get("published_at") or ""
    video_url = attrs.get("video_url") or ""
    description = attrs.get("description") or ""
    image_url = attrs.get("image_url") or ""
    transcript = attrs.get("transcript_text")
    issues: list[str] = []

    match = TITLE_RE.match(title)
    if not match:
        issues.append(f"标题格式错误，期望 E{{N}}. 标题：{title!r}")
    elif number is None:
        issues.append("episode.number 为空")
    elif int(match.group(1)) != int(number):
        issues.append(f"标题 E{match.group(1)} 与 episode.number={number} 不一致")

    if require_published and status != "published":
        issues.append(f"状态不是 published：{status!r}")
    if status == "published" and not published_at:
        issues.append("published_at 为空")
    if not video_url or not YOUTUBE_URL_RE.match(video_url):
        issues.append(f"video_url 缺失或格式异常：{video_url!r}")
    if not description.strip():
        issues.append("description 为空")
    if not image_url:
        issues.append("image_url 为空")

    if expected:
        expected_date = (expected.get("published_at") or "")[:10]
        actual_date = published_at[:10]
        if status == "published" and expected_date and actual_date != expected_date:
            issues.append(
                f"published_at 与本地 YouTube 日期不一致：expected={expected_date} actual={actual_date}"
            )
        expected_hash = expected.get("transcript_sha256")
        if expected_hash:
            if transcript is None and not (
                attrs.get("transcript_url") or attrs.get("transcripts")
            ):
                issues.append(
                    f"远端没有 transcript artifact，本地来源={expected.get('transcript_path')}"
                )
            elif transcript is not None and sha256_text(transcript or "") != expected_hash:
                issues.append(
                    f"transcript_text 与本地字幕不一致，本地来源={expected.get('transcript_path')}"
                )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10, help="检查按发布时间最新的 N 个")
    parser.add_argument("--episode-id")
    parser.add_argument("--video-id")
    parser.add_argument(
        "--allow-draft",
        action="store_true",
        help="允许目标 episode 是 draft（仍检查其他字段）",
    )
    parser.add_argument(
        "--collection-only",
        action="store_true",
        help="只用 episodes 集合快照做快速元数据汇总；不替代逐集 transcript 精确 GET",
    )
    parser.add_argument(
        "--only-failures",
        action="store_true",
        help="只打印有问题的 episode 和最终汇总",
    )
    args = parser.parse_args()

    load_env()
    api_key, show_id = require_transistor_config()
    client = TransistorClient(api_key)
    expectations = local_expectations()

    if args.episode_id:
        episodes = [client.get_episode(args.episode_id)]
    else:
        all_episodes = client.list_episodes(show_id)
        if args.video_id:
            episodes = [
                episode
                for episode in all_episodes
                if extract_video_id(episode.get("attributes", {}).get("video_url")) == args.video_id
            ]
        else:
            published = [
                episode
                for episode in all_episodes
                if episode.get("attributes", {}).get("status") == "published"
            ]
            episodes = sorted(
                published,
                key=lambda episode: episode.get("attributes", {}).get("published_at") or "",
                reverse=True,
            )[: args.n]

    if not episodes:
        print("❌ 没有找到目标 episode")
        return 1

    failed = 0
    for episode in episodes:
        episode_id = str(episode.get("id") or "")
        # Collection responses may omit transcript fields. Keep exact GET as the
        # default quality source; collection-only is an explicit fast metadata view.
        exact = episode if args.collection_only else client.get_episode(episode_id)
        attrs = exact.get("attributes", {})
        video_id = extract_video_id(attrs.get("video_url"))
        issues = check_episode(
            exact,
            expectations.get(video_id or ""),
            require_published=not args.allow_draft,
        )
        icon = "✅" if not issues else "❌"
        if issues or not args.only_failures:
            print(
                f"{icon} {episode_id}  {attrs.get('status')}  "
                f"{(attrs.get('published_at') or '')[:10]}  {attrs.get('title') or '(无标题)'}",
                flush=True,
            )
            for issue in issues:
                print(f"   - {issue}", flush=True)
        failed += bool(issues)

    print(f"\n检查 {len(episodes)} 个，失败 {failed} 个", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
