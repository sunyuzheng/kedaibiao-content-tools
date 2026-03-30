#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查待上传的视频候选
- 从 Transistor API 获取已发布的 episode 列表
- 扫描 archive/有人工字幕/ 下的本地视频
- 找出本地有但 Transistor 已发布列表中没有的视频（待上传）
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Set, Dict, Any

# 导入项目配置
sys.path.insert(0, str(Path(__file__).parent.parent / "upload"))
try:
    from upload_to_transistor_v2 import (
        TRANSISTOR_API_KEY,
        TRANSISTOR_API_BASE,
        TRANSISTOR_SHOW_ID,
        VIDEOS_WITH_SUBS_DIR,
    )
except ImportError as e:
    print(f"❌ 导入配置失败: {e}")
    sys.exit(1)

import requests

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
ARCHIVE_WITH_SUBS = PROJECT_ROOT / "archive" / "有人工字幕"


def extract_video_id_from_url(video_url: Optional[str]) -> Optional[str]:
    """从 YouTube video_url 中提取 video_id"""
    if not video_url:
        return None
    if "v=" in video_url:
        return video_url.split("v=")[-1].split("&")[0].strip()
    # 支持 youtu.be/xxx 格式
    if "youtu.be/" in video_url:
        return video_url.split("youtu.be/")[-1].split("?")[0].strip()
    return None


def extract_video_id_from_folder_name(folder_name: str) -> Optional[str]:
    """
    从文件夹名提取 video_id
    格式：YYYYMMDD_标题_VIDEO_ID
    YouTube ID 固定 11 位（可含 _ 和 -），直接取末尾 11 个字符
    """
    if len(folder_name) >= 11:
        video_id = folder_name[-11:]
        if re.match(r"^[a-zA-Z0-9_-]{11}$", video_id):
            return video_id
    return None


def extract_date_from_folder_name(folder_name: str) -> Optional[str]:
    """从文件夹名提取日期 YYYYMMDD"""
    parts = folder_name.split("_")
    if parts and len(parts[0]) == 8 and parts[0].isdigit():
        return parts[0]
    return None


def get_published_video_ids(show_id: str) -> Set[str]:
    """
    从 Transistor API 获取所有 status='published' 的 episode，
    提取 video_url 中的 video_id 集合
    """
    published_ids: Set[str] = set()
    page_num = 1
    per_page = 50

    while True:
        url = f"{TRANSISTOR_API_BASE}/episodes"
        params = {
            "show_id": show_id,
            "pagination[page]": page_num,
            "pagination[per]": per_page,
        }
        headers = {"x-api-key": TRANSISTOR_API_KEY}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            episodes = data.get("data", [])

            if not episodes:
                break

            for ep in episodes:
                attrs = ep.get("attributes", {})
                if attrs.get("status") != "published":
                    continue
                video_url = attrs.get("video_url")
                vid = extract_video_id_from_url(video_url)
                if vid:
                    published_ids.add(vid)

            meta = data.get("meta", {})
            total_pages = meta.get("totalPages", 1)
            current_page = meta.get("currentPage", page_num)

            if current_page >= total_pages:
                break

            page_num += 1
            time.sleep(1.2)

        except Exception as e:
            print(f"❌ 获取 episode 失败 (page {page_num}): {e}", file=sys.stderr)
            if hasattr(e, "response") and getattr(e, "response", None) is not None:
                print(f"   响应: {e.response.text[:500]}", file=sys.stderr)
            break

    return published_ids


def scan_local_folders(archive_dir: Path) -> List[Dict[str, Any]]:
    """
    扫描 archive/有人工字幕/ 下所有子文件夹，
    提取 folder_name, video_id, date
    """
    candidates: List[Dict[str, Any]] = []
    if not archive_dir.exists():
        return candidates

    for item in archive_dir.iterdir():
        if not item.is_dir():
            continue
        folder_name = item.name
        video_id = extract_video_id_from_folder_name(folder_name)
        if not video_id:
            continue
        date_str = extract_date_from_folder_name(folder_name)
        candidates.append({
            "folder_name": folder_name,
            "video_id": video_id,
            "date": date_str or "",
        })

    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(
        description="检查待上传的视频候选（本地有但 Transistor 已发布列表中没有的）"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出完整列表，供后续上传脚本使用（进度信息输出到 stderr）",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=ARCHIVE_WITH_SUBS,
        help=f"archive 有人工字幕 目录 (默认: {ARCHIVE_WITH_SUBS})",
    )
    parser.add_argument(
        "--show-id",
        default=TRANSISTOR_SHOW_ID,
        help=f"Transistor show ID (默认: {TRANSISTOR_SHOW_ID})",
    )
    args = parser.parse_args()

    def log(msg: str) -> None:
        """输出到 stderr（--json 时）或 stdout（默认）"""
        if args.json:
            print(msg, file=sys.stderr)
        else:
            print(msg)

    # 1. 获取已发布的 video_id 集合
    log("📥 正在从 Transistor API 获取已发布的 episode...")
    published_ids = get_published_video_ids(args.show_id)
    log(f"   已发布: {len(published_ids)} 个视频")

    # 2. 扫描本地文件夹
    log(f"📂 正在扫描 {args.archive_dir} ...")
    local_items = scan_local_folders(args.archive_dir)
    log(f"   本地有字幕视频: {len(local_items)} 个")

    # 3. 找出已发布视频中本地有对应文件夹的最新日期
    #    只上传比这个日期更新的视频，避免旧的漏网视频混进来
    published_local_dates = [
        item["date"] for item in local_items
        if item["video_id"] in published_ids and item["date"]
    ]
    max_published_date = max(published_local_dates) if published_local_dates else ""
    log(f"   本地已发布的最新视频日期: {max_published_date}")

    # 4. 待上传 = 本地有 & Transistor 没有 & 比已发布最新内容更新
    pending = [
        item
        for item in local_items
        if item["video_id"] not in published_ids
        and item["date"] > max_published_date
    ]

    # 5. 按日期升序排序（旧的先上传，新的拿到更大的 EP 号）
    pending.sort(key=lambda x: x["folder_name"])

    # 6. 输出
    if args.json:
        output = {
            "count": len(pending),
            "candidates": pending,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 默认文本输出
    print()
    print(f"📋 待上传数量: {len(pending)}")
    print()
    if pending:
        print("待上传列表（按日期升序，旧的先传，新的 EP 号最大）:")
        print("-" * 80)
        for i, item in enumerate(pending, 1):
            print(f"  {i:3}. {item['folder_name']}")
            print(f"      video_id: {item['video_id']}  日期: {item['date']}")
        print("-" * 80)
        print(f"共 {len(pending)} 个待上传")
    else:
        print("✅ 没有待上传的视频，本地内容已全部同步到 Transistor。")


if __name__ == "__main__":
    main()
