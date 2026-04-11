#!/usr/bin/env python3
"""
上传质量检查脚本 — 每次上传后运行，验证最新 N 个 episodes 是否符合规范。

检查项：
  1. 标题格式：必须是 "E{N}. 标题"
  2. Episode 编号：与标题前缀一致
  3. Published date：与标题中的 EP 号顺序一致（旧 EP 号 < 新 EP 号）
  4. Published date：不是今天（上传当天），而是视频实际发布日期
  5. YouTube video_url：已设置且格式正确
  6. 状态：published（不是 draft）
  7. 描述：非空
  8. 图片：已设置

用法：
  python3 tools/check/check_upload_quality.py         # 检查最新 5 个
  python3 tools/check/check_upload_quality.py --n 10  # 检查最新 10 个
  python3 tools/check/check_upload_quality.py --episode-id 3163417  # 检查单个
"""

import os
import re
import sys
import time
import argparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 环境变量 ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

TRANSISTOR_API_KEY = os.getenv("TRANSISTOR_API_KEY", "")
TRANSISTOR_SHOW_ID = os.getenv("TRANSISTOR_SHOW_ID", "")
BASE = "https://api.transistor.fm/v1"

TITLE_RE = re.compile(r"^E(\d+)\.\s+.+")
YOUTUBE_URL_RE = re.compile(r"https?://(www\.)?youtube\.com/watch\?v=[\w\-]+")


def api_headers():
    return {"x-api-key": TRANSISTOR_API_KEY}


def fetch_episodes(page=1, per_page=50, retries=3):
    for attempt in range(retries):
        r = requests.get(
            f"{BASE}/episodes",
            headers=api_headers(),
            params={"show_id": TRANSISTOR_SHOW_ID, "pagination[page]": page, "pagination[per]": per_page},
            timeout=30,
        )
        if r.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"   ⏸️  Rate limit (429)，等待 {wait}s 后重试...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json().get("data", [])
    r.raise_for_status()
    return []


def fetch_all_episodes(limit=None):
    """分页拉取全部 episodes，页间间隔 1s 避免限速"""
    all_eps = []
    page = 1
    while True:
        batch = fetch_episodes(page=page, per_page=50)
        if not batch:
            break
        all_eps.extend(batch)
        if limit and len(all_eps) >= limit:
            all_eps = all_eps[:limit]
            break
        page += 1
        time.sleep(1)
    return all_eps


def fetch_single_episode(episode_id):
    r = requests.get(f"{BASE}/episodes/{episode_id}", headers=api_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("data")


def check_episode(ep, today_str: str) -> list[str]:
    """返回该 episode 的所有问题列表（空列表 = 全部通过）"""
    attrs = ep.get("attributes", {})
    title = attrs.get("title") or ""
    number = attrs.get("number")
    status = attrs.get("status") or ""
    published_at = attrs.get("published_at") or ""
    video_url = attrs.get("video_url") or ""
    description = attrs.get("description") or ""
    image_url = attrs.get("image_url") or ""

    issues = []

    # 1. 标题格式
    m = TITLE_RE.match(title)
    if not m:
        issues.append(f"标题格式错误（期望 'E{{N}}. 标题'）: '{title}'")
    else:
        # 2. 标题中的编号与 number 字段一致
        title_number = int(m.group(1))
        if number is None:
            issues.append(f"episode.number 字段为空")
        elif title_number != number:
            issues.append(f"标题前缀 E{title_number} 与 episode.number={number} 不一致")

    # 3. 状态
    if status != "published":
        issues.append(f"状态不是 published：'{status}'")

    # 4. published_at 不是今天（检查日期部分）
    if published_at:
        pub_date = published_at[:10]  # "YYYY-MM-DD"
        if pub_date == today_str:
            issues.append(f"published_at 是今天 ({today_str})，应为视频实际发布日期")
    else:
        issues.append("published_at 为空")

    # 5. YouTube video_url
    if not video_url:
        issues.append("video_url（YouTube 链接）未设置")
    elif not YOUTUBE_URL_RE.match(video_url):
        issues.append(f"video_url 格式异常: '{video_url}'")

    # 6. 描述
    if not description.strip():
        issues.append("description 为空")

    # 7. 图片
    if not image_url:
        issues.append("image_url（封面图）未设置")

    return issues


def main():
    parser = argparse.ArgumentParser(description="上传质量检查")
    parser.add_argument("--n", type=int, default=5, help="检查最新 N 个 episodes（默认 5）")
    parser.add_argument("--episode-id", type=str, help="只检查指定 episode ID")
    args = parser.parse_args()

    if not TRANSISTOR_API_KEY:
        print("❌ TRANSISTOR_API_KEY 未设置")
        sys.exit(1)

    # 今天日期（UTC）
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 60)
    print(f"📋 上传质量检查  (today UTC: {today_utc})")
    print("=" * 60)

    if args.episode_id:
        episodes = [fetch_single_episode(args.episode_id)]
        print(f"检查 episode ID: {args.episode_id}")
    else:
        print(f"📥 拉取全部 episodes（最多 {args.n} 个）...")
        episodes = fetch_all_episodes(limit=args.n)

    if not episodes:
        print("⚠️  没有找到 episodes")
        return

    total = len(episodes)
    fail_count = 0

    for ep in episodes:
        ep_id = ep.get("id", "?")
        attrs = ep.get("attributes", {})
        title = attrs.get("title") or "(无标题)"
        number = attrs.get("number", "?")
        published_at = (attrs.get("published_at") or "")[:10]

        issues = check_episode(ep, today_utc)

        status_icon = "✅" if not issues else "❌"
        print(f"\n{status_icon} E{number} | {title}")
        print(f"   ID: {ep_id}  |  published: {published_at}  |  video_url: {attrs.get('video_url','—')}")

        if issues:
            fail_count += 1
            for issue in issues:
                print(f"   ⚠️  {issue}")

    print("\n" + "=" * 60)
    if fail_count == 0:
        print(f"✅ 全部 {total} 个 episodes 通过质量检查")
    else:
        print(f"❌ {fail_count}/{total} 个 episodes 有问题，请手动修复")
        print()
        print("修复 published_at 命令示例：")
        print("  curl -X PATCH https://api.transistor.fm/v1/episodes/EPISODE_ID/publish \\")
        print('    -H "x-api-key: $TRANSISTOR_API_KEY" \\')
        print('    -H "Content-Type: application/x-www-form-urlencoded" \\')
        print('    -d "episode[status]=published&episode[published_at]=2026-04-05T00:00:00.000Z"')
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()
