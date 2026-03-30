#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 YouTube 视频发布时间重新排序 Transistor 上的 episodes。

逻辑：
1. 从 Transistor API 获取所有 published episodes
2. 从本地 archive 目录建立 video_id -> YYYYMMDD 日期映射
3. 按日期升序排序（最早 = EP1，最新 = EP最大）
4. 通过 PATCH API 更新每个 episode 的 number 字段

自包含，不依赖任何外部 JSON 文件。
"""

import os
import re
import requests
import time
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 从 .env 加载环境变量（无需 python-dotenv）
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Transistor.fm API 配置
TRANSISTOR_API_KEY = os.environ["TRANSISTOR_API_KEY"]
TRANSISTOR_API_BASE = "https://api.transistor.fm/v1"
TRANSISTOR_SHOW_ID = os.environ["TRANSISTOR_SHOW_ID"]

PROJECT_ROOT = Path(__file__).parent.parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "archive"


def extract_video_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if "v=" in url:
        return url.split("v=")[-1].split("&")[0].strip()
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0].strip()
    return None


def build_local_date_map() -> Dict[str, str]:
    """从本地 archive 文件夹名提取 video_id -> YYYYMMDD 映射。"""
    date_map: Dict[str, str] = {}
    for subdir in ["有人工字幕", "无人工字幕"]:
        d = ARCHIVE_DIR / subdir
        if not d.exists():
            continue
        for folder in d.iterdir():
            if not folder.is_dir():
                continue
            name = folder.name
            # 日期：前8位数字
            date = name[:8] if len(name) >= 8 and name[:8].isdigit() else None
            # video_id：末尾11位
            vid_id = name[-11:] if len(name) >= 11 else None
            if date and vid_id and re.match(r"^[a-zA-Z0-9_-]{11}$", vid_id):
                date_map[vid_id] = date
    return date_map


def get_all_published_episodes() -> List[Dict]:
    """从 Transistor API 获取所有 published episodes（分页）。"""
    episodes = []
    page = 1
    headers = {"x-api-key": TRANSISTOR_API_KEY}
    print("📥 从 Transistor API 获取所有已发布 episodes...")
    while True:
        resp = requests.get(
            f"{TRANSISTOR_API_BASE}/episodes",
            headers=headers,
            params={"show_id": TRANSISTOR_SHOW_ID, "pagination[page]": page, "pagination[per]": 50},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = [ep for ep in data.get("data", []) if ep.get("attributes", {}).get("status") == "published"]
        episodes.extend(batch)
        meta = data.get("meta", {})
        print(f"   第 {page} 页：{len(batch)} 个，累计 {len(episodes)} 个")
        if page >= meta.get("totalPages", 1):
            break
        page += 1
        time.sleep(1.2)
    return episodes


def update_episode_number(episode_id: str, number: int) -> bool:
    headers = {"x-api-key": TRANSISTOR_API_KEY, "Content-Type": "application/json"}
    for attempt in range(3):
        resp = requests.patch(
            f"{TRANSISTOR_API_BASE}/episodes/{episode_id}",
            headers=headers,
            json={"episode": {"number": number}},
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"   ⏸️  限流，等待 {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code in [200, 201]:
            return True
        print(f"   ⚠️  更新失败: HTTP {resp.status_code} - {resp.text[:200]}")
        return False
    return False


def reorder(dry_run: bool = False) -> None:
    # 1. 获取本地日期映射
    local_dates = build_local_date_map()
    print(f"📂 本地 archive 中找到 {len(local_dates)} 个视频的日期信息")

    # 2. 获取所有已发布 episodes
    episodes = get_all_published_episodes()
    print(f"📊 Transistor 上共 {len(episodes)} 个已发布 episodes")

    # 3. 为每个 episode 查找 YouTube 发布日期
    records = []
    no_date_count = 0
    for ep in episodes:
        attrs = ep.get("attributes", {})
        video_url = attrs.get("video_url", "")
        vid_id = extract_video_id_from_url(video_url)
        date = local_dates.get(vid_id, "") if vid_id else ""
        if not date:
            no_date_count += 1
        records.append({
            "episode_id": ep["id"],
            "current_number": attrs.get("number"),
            "title": attrs.get("title", "")[:60],
            "video_id": vid_id,
            "date": date,
        })

    print(f"   ✅ 有日期: {len(records) - no_date_count}  ⚠️  无日期: {no_date_count}")

    # 4. 排序：有日期的按日期升序；无日期的按当前编号升序（排最前）
    records.sort(key=lambda x: (x["date"] if x["date"] else "00000000", x["current_number"] or 0))

    # 5. dry-run 预览
    if dry_run:
        print("\n🔍 试运行模式 — 前20 / 后10 预览:\n")
        for i, r in enumerate(records[:20], 1):
            mark = "→" if r["current_number"] != i else " "
            print(f"  {mark} EP{i:4d}  [{r['date'] or '无日期':8s}]  {r['title']}")
        print("  ...")
        for i, r in enumerate(records[-10:], len(records) - 9):
            mark = "→" if r["current_number"] != i else " "
            print(f"  {mark} EP{i:4d}  [{r['date'] or '无日期':8s}]  {r['title']}")
        return

    # 6. 更新
    print(f"\n🔄 开始更新 episode 编号（共 {len(records)} 个）...")
    success = skip = fail = 0
    for i, r in enumerate(records, 1):
        ep_id = r["episode_id"]
        if r["current_number"] == i:
            skip += 1
            continue
        print(f"  EP{r['current_number']:4d} → EP{i:4d}  [{r['date'] or '无日期':8s}]  {r['title']}")
        if update_episode_number(ep_id, i):
            success += 1
        else:
            fail += 1
        time.sleep(1.2)

    print(f"\n{'='*50}")
    print(f"✅ 更新: {success}  ⏭️  跳过: {skip}  ❌ 失败: {fail}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="按 YouTube 发布时间重排 Transistor episodes")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际更新")
    args = parser.parse_args()
    reorder(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
