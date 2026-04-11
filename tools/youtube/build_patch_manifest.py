#!/usr/bin/env python3
"""
构建「视频 ID → 新描述」的 patch_manifest.json

逻辑：
  - 遍历 guests.json 中每位嘉宾的所有视频 ID
  - 从本地 archive 找到对应 .info.json，读取现有 description
  - 如果描述末尾已含 lizheng.ai/guests，跳过（幂等）
  - 否则生成「嘉宾信息块」并追加
  - 输出 patch_manifest.json（dry_run / apply 共用）

运行：
    python3 tools/youtube/build_patch_manifest.py
    python3 tools/youtube/build_patch_manifest.py --force   # 重新生成已有的 manifest
"""

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
GUESTS_JSON = PROJECT_ROOT / "guests.json"
ALL_VIDEOS_FILE = PROJECT_ROOT / "tools/youtube/all_videos_full.json"
MANIFEST_FILE = PROJECT_ROOT / "tools/youtube/patch_manifest.json"
GUESTS_PAGE_URL = "https://www.lizheng.ai/guests"
COMMUNITY_URL = "https://www.superlinear.academy"
ALREADY_PATCHED_MARKER = GUESTS_PAGE_URL


def build_video_id_index() -> dict[str, dict]:
    """从 all_videos_full.json 建立 video_id → 视频数据 的索引。
    这是以 YouTube API 为权威来源的当前真实描述。
    """
    if not ALL_VIDEOS_FILE.exists():
        raise FileNotFoundError(
            "tools/youtube/all_videos_full.json 不存在，请先运行：\n"
            "  envs/youtube_env/bin/python3 tools/youtube/fetch_all_videos.py"
        )
    videos = json.loads(ALL_VIDEOS_FILE.read_text(encoding="utf-8"))
    return {v["video_id"]: v for v in videos}


def make_guest_block(guest: dict) -> str:
    """生成追加到描述末尾的嘉宾信息块。"""
    name = guest["guest_name"]
    en_name = guest.get("guest_en_name", "")
    title = guest.get("guest_title", "")
    company = guest.get("guest_company", "")

    lines = ["", "─" * 36]

    # 姓名行
    if en_name and en_name != name:
        lines.append(f"嘉宾 / Guest：{name}（{en_name}）")
    else:
        lines.append(f"嘉宾 / Guest：{name}")

    # 职位 & 公司
    if title and company:
        lines.append(f"{title} @ {company}")
    elif title:
        lines.append(title)
    elif company:
        lines.append(company)

    lines.append(f"更多嘉宾访谈：{GUESTS_PAGE_URL}")
    lines.append(f"加入课代表社区：{COMMUNITY_URL}")
    lines.append("─" * 36)

    return "\n".join(lines)


def build_manifest(force: bool = False) -> list[dict]:
    print("读取 YouTube API 视频数据（all_videos_full.json）...")
    id_index = build_video_id_index()
    print(f"  找到 {len(id_index)} 个视频（YouTube API 权威来源）")

    guests = json.loads(GUESTS_JSON.read_text(encoding="utf-8"))
    print(f"  嘉宾数：{len(guests)}")

    patches = []
    skipped_already_done = 0
    skipped_no_local = 0

    for guest in guests:
        guest_block = make_guest_block(guest)
        for vid in guest.get("all_video_ids", []):
            if vid not in id_index:
                skipped_no_local += 1
            # 视频在 guests.json 中但 YouTube API 找不到（可能已删除）
                continue

            video_data = id_index[vid]
            existing_desc = video_data.get("description", "") or ""

            if not force and ALREADY_PATCHED_MARKER in existing_desc:
                skipped_already_done += 1
                continue

            new_desc = existing_desc.rstrip() + "\n" + guest_block

            patches.append({
                "video_id": vid,
                "guest_name": guest["guest_name"],
                "old_description": existing_desc,
                "new_description": new_desc,
                "status": "pending",  # pending | done | error
            })

    print(f"\n结果：")
    print(f"  需要更新：{len(patches)} 个视频")
    print(f"  跳过（已处理）：{skipped_already_done}")
    print(f"  跳过（无本地文件）：{skipped_no_local}")
    return patches


def main():
    parser = argparse.ArgumentParser(description="构建 YouTube 描述 patch manifest")
    parser.add_argument("--force", action="store_true", help="重新生成已包含嘉宾信息的描述")
    args = parser.parse_args()

    patches = build_manifest(force=args.force)

    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(
        json.dumps(patches, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n✓ manifest 已写入：{MANIFEST_FILE}")
    print(f"  下一步：python3 tools/youtube/dry_run.py")


if __name__ == "__main__":
    main()
