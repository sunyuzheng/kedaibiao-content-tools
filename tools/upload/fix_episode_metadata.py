#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复所有已发布 episodes 的 image_url 和 published_at。

- image_url: 填充缺失的缩略图（YouTube CDN URL）
- published_at: 按 YouTube 发布日期设置，保证 podcast feed 顺序正确

运行方式:
  python3 fix_episode_metadata.py --dry-run   # 预览，不实际修改
  python3 fix_episode_metadata.py             # 正式运行
  python3 fix_episode_metadata.py --from-ep 441  # 从指定 EP 开始（断点续传）
"""

import os
import re
import requests
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict, List, Optional

# 从 .env 加载环境变量（无需 python-dotenv）
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

TRANSISTOR_API_KEY = os.environ["TRANSISTOR_API_KEY"]
TRANSISTOR_API_BASE = "https://api.transistor.fm/v1"
TRANSISTOR_SHOW_ID = os.environ["TRANSISTOR_SHOW_ID"]

PROJECT_ROOT = Path(__file__).parent.parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "archive"


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
            date = name[:8] if len(name) >= 8 and name[:8].isdigit() else None
            vid_id = name[-11:] if len(name) >= 11 else None
            if date and vid_id and re.match(r"^[a-zA-Z0-9_-]{11}$", vid_id):
                date_map[vid_id] = date
    return date_map


def extract_video_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if "v=" in url:
        return url.split("v=")[-1].split("&")[0].strip()
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0].strip()
    return None


def get_all_published_episodes() -> List[Dict]:
    episodes = []
    page = 1
    headers = {"x-api-key": TRANSISTOR_API_KEY}
    print("📥 获取所有已发布 episodes...")
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
        print(f"   第 {page} 页: {len(batch)} 个，累计 {len(episodes)} 个")
        if page >= meta.get("totalPages", 1):
            break
        page += 1
        time.sleep(1.2)
    return episodes


def api_patch(ep_id: str, payload: Dict) -> tuple:
    """PATCH /v1/episodes/:id — 更新元数据（image_url, number 等）"""
    headers = {"x-api-key": TRANSISTOR_API_KEY, "Content-Type": "application/json"}
    for attempt in range(3):
        resp = requests.patch(
            f"{TRANSISTOR_API_BASE}/episodes/{ep_id}",
            headers=headers,
            json={"episode": payload},
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"\n   ⏸️  限流，等待 {wait}s...")
            time.sleep(wait)
            continue
        return resp.status_code, resp.text
    return 429, "rate limit"


def api_publish(ep_id: str, status: str, published_at: Optional[str] = None) -> tuple:
    """PATCH /v1/episodes/:id/publish — 改变发布状态，可同时设置 published_at"""
    headers = {"x-api-key": TRANSISTOR_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    data = f"episode[status]={status}"
    if published_at:
        data += f"&episode[published_at]={published_at}"
    for attempt in range(3):
        resp = requests.patch(
            f"{TRANSISTOR_API_BASE}/episodes/{ep_id}/publish",
            headers=headers,
            data=data,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"\n   ⏸️  限流，等待 {wait}s...")
            time.sleep(wait)
            continue
        return resp.status_code, resp.text
    return 429, "rate limit"


def build_records(episodes: List[Dict], local_dates: Dict[str, str]) -> List[Dict]:
    """按 EP 号排序，为每个 episode 计算目标 published_at 和 image_url。"""
    episodes.sort(key=lambda e: e.get("attributes", {}).get("number") or 0)

    # 每个日期内按 EP 号顺序分配分钟偏移，保证同日视频顺序正确
    day_counter: Dict[str, int] = defaultdict(int)
    records = []
    for ep in episodes:
        attrs = ep.get("attributes", {})
        ep_num = attrs.get("number")
        vid_url = attrs.get("video_url", "")
        vid_id = extract_video_id(vid_url)
        date = local_dates.get(vid_id, "") if vid_id else ""
        current_image = attrs.get("image_url") or ""
        current_pub_at = attrs.get("published_at") or ""

        # 目标 published_at: YYYYMMDD → YYYY-MM-DDT00:MM:00+00:00（每日内按EP顺序每隔1分钟）
        if date and len(date) == 8:
            idx = day_counter[date]
            day_counter[date] += 1
            dt = datetime(int(date[:4]), int(date[4:6]), int(date[6:8]),
                          tzinfo=timezone.utc) + timedelta(minutes=idx)
            target_pub_at = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            target_pub_at = None

        # 目标 image_url: 只有缺失时才填充（已有的保持不变）
        # 用 hqdefault.jpg，maxresdefault.jpg 对旧视频可能不存在（404），Transistor 会静默拒绝
        if not current_image:
            target_image = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else None
        else:
            target_image = None  # 已有，跳过

        records.append({
            "ep_id": ep["id"],
            "ep_num": ep_num,
            "vid_id": vid_id,
            "date": date,
            "current_pub_at": current_pub_at,
            "target_pub_at": target_pub_at,
            "current_image": current_image,
            "target_image": target_image,
        })
    return records


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="修复 episode 的 image_url 和 published_at")
    parser.add_argument("--dry-run", action="store_true", help="预览，不实际修改")
    parser.add_argument("--from-ep", type=int, default=1, help="从哪个 EP 号开始（断点续传）")
    parser.add_argument("--image-only", action="store_true", help="只修复 image_url")
    parser.add_argument("--date-only", action="store_true", help="只修复 published_at")
    args = parser.parse_args()

    local_dates = build_local_date_map()
    print(f"📂 本地 archive: {len(local_dates)} 个视频")

    episodes = get_all_published_episodes()
    print(f"📊 共 {len(episodes)} 个已发布 episodes")

    records = build_records(episodes, local_dates)

    no_date = sum(1 for r in records if not r["date"])
    need_image = sum(1 for r in records if r["target_image"])
    print(f"\n统计: 需修复缩略图={need_image}  无本地日期={no_date}")

    if args.dry_run:
        print("\n🔍 dry-run 预览 (全部):\n")
        print(f"  {'EP':>5}  {'日期':8}  {'当前pub_at':30}  {'目标pub_at':30}  {'缩略图'}")
        print(f"  {'-'*5}  {'-'*8}  {'-'*30}  {'-'*30}  {'-'*6}")
        for r in records:
            img_note = "补充" if r["target_image"] else ("有" if r["current_image"] else "无")
            print(f"  EP{r['ep_num']:4d}  {r['date'] or '????':8}  {r['current_pub_at'][:30]:30}  {(r['target_pub_at'] or 'skip'):30}  {img_note}")
        return

    # 正式运行
    img_ok = img_skip = img_fail = 0
    pub_ok = pub_skip = pub_fail = 0

    for r in records:
        if (r["ep_num"] or 0) < args.from_ep:
            continue

        line = f"EP{r['ep_num']:4d} [{r['date'] or '无日期':8}] {r['vid_id'] or '':11}"

        # 1. 修复 image_url（只处理缺失的）
        if not args.date_only:
            if r["target_image"]:
                code, text = api_patch(r["ep_id"], {"image_url": r["target_image"]})
                if code in [200, 201]:
                    img_ok += 1
                    line += "  img:✅"
                else:
                    img_fail += 1
                    line += f"  img:❌({code})"
                time.sleep(1.2)
            else:
                img_skip += 1
                line += "  img:–"

        # 2. 修复 published_at（设置正确日期）
        if not args.image_only and r["target_pub_at"]:
            code, text = api_publish(r["ep_id"], "published", r["target_pub_at"])
            if code in [200, 201]:
                pub_ok += 1
                line += "  pub:✅"
            else:
                pub_fail += 1
                line += f"  pub:❌({code}) {text[:80]}"
            time.sleep(1.2)
        elif not args.image_only:
            pub_skip += 1
            line += "  pub:–(无日期)"

        print(line)

    print(f"\n{'='*60}")
    print(f"image_url  : 修复={img_ok}  跳过={img_skip}  失败={img_fail}")
    print(f"published_at: 修复={pub_ok}  跳过={pub_skip}  失败={pub_fail}")


if __name__ == "__main__":
    main()
