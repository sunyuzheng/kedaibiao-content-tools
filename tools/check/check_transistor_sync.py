#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全量对照本地 archive 与 Transistor 的同步状态（live 查询，不生成文件）。

用途：发现有人工字幕但从未上传的视频，包括历史漏网视频。
     日常上传用 check_upload_candidates.py；全量审计时用本脚本。

用法：
  python3 tools/check/check_transistor_sync.py
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "archive"

sys.path.insert(0, str(PROJECT_ROOT / "tools" / "upload"))
try:
    from upload_to_transistor_v2 import TRANSISTOR_API_KEY, TRANSISTOR_API_BASE, TRANSISTOR_SHOW_ID
except ImportError as e:
    print(f"❌ 导入配置失败: {e}")
    sys.exit(1)

import requests


def load_local_videos() -> List[Dict]:
    videos = []
    for subdir in ["有人工字幕", "无人工字幕"]:
        d = ARCHIVE_DIR / subdir
        if not d.exists():
            continue
        for folder in d.iterdir():
            if not folder.is_dir():
                continue
            info_files = list(folder.glob("*.info.json"))
            if not info_files:
                continue
            try:
                with open(info_files[0], encoding="utf-8") as f:
                    data = json.load(f)
                if not list(folder.glob("*.m4a")):
                    continue
                videos.append({
                    "title": data.get("title", ""),
                    "upload_date": data.get("upload_date", ""),
                    "video_id": data.get("id", ""),
                    "category": subdir,
                })
            except Exception:
                pass
    return videos


def get_transistor_map() -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    page = 1
    headers = {"x-api-key": TRANSISTOR_API_KEY}
    while True:
        resp = requests.get(
            f"{TRANSISTOR_API_BASE}/episodes",
            headers=headers,
            params={"show_id": TRANSISTOR_SHOW_ID, "pagination[page]": page, "pagination[per]": 50},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for ep in data.get("data", []):
            attrs = ep.get("attributes", {})
            url = attrs.get("video_url", "")
            vid = None
            if "v=" in url:
                vid = url.split("v=")[-1].split("&")[0].strip()
            elif "youtu.be/" in url:
                vid = url.split("youtu.be/")[-1].split("?")[0].strip()
            if not vid or len(vid) != 11:
                continue
            status = attrs.get("status", "")
            num = attrs.get("number")
            if vid not in result or (status == "published" and result[vid]["status"] != "published"):
                result[vid] = {"number": num, "status": status}
        if page >= data.get("meta", {}).get("totalPages", 1):
            break
        page += 1
        time.sleep(1.2)
    return result


def fmt(date_str: str) -> str:
    try:
        return datetime.strptime(date_str[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return date_str or ""


def main() -> None:
    print("📥 扫描本地 archive...")
    videos = load_local_videos()
    print(f"   本地视频（有音频）: {len(videos)} 个")

    print("📡 从 Transistor API 获取 episodes...")
    tr_map = get_transistor_map()
    published = sum(1 for v in tr_map.values() if v["status"] == "published")
    print(f"   Transistor 有 video_url 的 episodes: {len(tr_map)} 个（{published} 个已发布）")

    videos.sort(key=lambda v: v.get("upload_date", ""), reverse=True)

    not_uploaded = [v for v in videos if v["video_id"] not in tr_map]
    with_subs_missing = [v for v in not_uploaded if v["category"] == "有人工字幕"]
    no_subs_missing = [v for v in not_uploaded if v["category"] == "无人工字幕"]

    print(f"\n{'='*60}")
    print(f"本地已下载:       {len(videos):>4} 个")
    print(f"已在 Transistor:  {len(videos) - len(not_uploaded):>4} 个")
    print(f"未上传:           {len(not_uploaded):>4} 个")
    print(f"  有人工字幕未上传: {len(with_subs_missing):>3} 个  ← 可直接上传")
    print(f"  无人工字幕未上传: {len(no_subs_missing):>3} 个  ← 需先转录")

    if with_subs_missing:
        print(f"\n⚠️  有人工字幕但未上传（共 {len(with_subs_missing)} 个）：")
        for v in with_subs_missing:
            print(f"   {fmt(v['upload_date'])}  {v['title'][:55]}  ({v['video_id']})")

    recent_no_subs = [v for v in no_subs_missing if v.get("upload_date", "") >= "20250101"]
    if recent_no_subs:
        print(f"\n📋 无人工字幕未上传（2025 年后，{len(recent_no_subs)} 个，需转录）：")
        for v in recent_no_subs:
            print(f"   {fmt(v['upload_date'])}  {v['title'][:55]}  ({v['video_id']})")


if __name__ == "__main__":
    main()
