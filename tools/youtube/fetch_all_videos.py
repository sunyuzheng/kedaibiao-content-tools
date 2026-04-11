#!/usr/bin/env python3
"""
从 YouTube API 拉取频道所有视频的完整元数据，保存到 all_videos_full.json。

这是 build_patch_manifest.py 的数据来源（权威来源，比本地 archive 更准确）。

配额消耗：
  - playlistItems.list: ⌈N/50⌉ units（拉视频 ID）
  - videos.list:        ⌈N/50⌉ units（拉元数据）
  - 约 922 个视频 ≈ 38 units，极省

用法：
    envs/youtube_env/bin/python3 tools/youtube/fetch_all_videos.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.youtube.auth import get_youtube_client

OUT_FILE = Path(__file__).parent / "all_videos_full.json"


def fetch_all_videos():
    yt = get_youtube_client()

    # 获取 Uploads 播放列表 ID
    ch = yt.channels().list(
        part="contentDetails,statistics", mine=True
    ).execute()["items"][0]
    uploads_id = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    print(f"频道视频数（公开）: {ch['statistics']['videoCount']}")
    print(f"开始从 Uploads 播放列表拉取所有视频 ID...")

    # 分页拉取所有 video ID
    all_ids = []
    page_token = None
    while True:
        kwargs = dict(part="contentDetails", playlistId=uploads_id, maxResults=50)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = yt.playlistItems().list(**kwargs).execute()
        all_ids.extend(
            item["contentDetails"]["videoId"] for item in resp.get("items", [])
        )
        page_token = resp.get("nextPageToken")
        print(f"  已拉取 {len(all_ids)} 个 ID", end="\r")
        if not page_token:
            break

    print(f"\n播放列表共 {len(all_ids)} 条（含会员/私密）")

    # 批量拉取完整元数据（50个/call）
    print("批量拉取元数据...")
    all_videos = []
    for i in range(0, len(all_ids), 50):
        batch = all_ids[i : i + 50]
        resp = yt.videos().list(
            part="snippet,statistics,status,contentDetails",
            id=",".join(batch),
        ).execute()
        all_videos.extend(resp.get("items", []))
        print(f"  {min(i+50, len(all_ids))}/{len(all_ids)}", end="\r")

    print(f"\n获取元数据 {len(all_videos)} 条")

    # 整理为精简格式
    clean = []
    for v in all_videos:
        s = v["snippet"]
        stats = v.get("statistics", {})
        clean.append(
            {
                "video_id": v["id"],
                "title": s["title"],
                "description": s.get("description", ""),
                "published_at": s["publishedAt"],
                "privacy": v["status"]["privacyStatus"],
                "duration": v.get("contentDetails", {}).get("duration", ""),
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats.get("likeCount", 0)),
                "comment_count": int(stats.get("commentCount", 0)),
                "category_id": s.get("categoryId", ""),
                "default_language": s.get("defaultLanguage", ""),
                "tags": s.get("tags", []),
            }
        )

    clean.sort(key=lambda x: x["published_at"])

    OUT_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2))

    from collections import Counter
    privacy = Counter(v["privacy"] for v in clean)
    print(f"\n已保存: {OUT_FILE}  ({OUT_FILE.stat().st_size // 1024} KB)")
    print(f"隐私状态: public={privacy['public']}, unlisted={privacy['unlisted']}, private={privacy['private']}")
    print(f"最早: {clean[0]['published_at'][:10]}  {clean[0]['title'][:50]}")
    print(f"最新: {clean[-1]['published_at'][:10]}  {clean[-1]['title'][:50]}")


if __name__ == "__main__":
    fetch_all_videos()
