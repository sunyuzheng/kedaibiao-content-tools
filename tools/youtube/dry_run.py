#!/usr/bin/env python3
"""
预览 patch_manifest.json 的变更，不写入 YouTube。

用法：
    python3 tools/youtube/dry_run.py               # 显示所有 pending 条目
    python3 tools/youtube/dry_run.py --n 5         # 只显示前 5 条
    python3 tools/youtube/dry_run.py --guest Leon  # 只显示特定嘉宾
"""

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
MANIFEST_FILE = PROJECT_ROOT / "tools/youtube/patch_manifest.json"


def show_diff(old: str, new: str):
    """打印 before/after diff（仅显示追加的部分）。"""
    added = new[len(old):]
    print("  【现有描述】")
    preview = old.strip()
    if len(preview) > 300:
        preview = preview[:300] + "..."
    for line in preview.splitlines():
        print(f"    {line}")
    print("  【追加内容】")
    for line in added.strip().splitlines():
        print(f"  + {line}")


def main():
    parser = argparse.ArgumentParser(description="预览 YouTube 描述变更")
    parser.add_argument("--n", type=int, default=0, help="最多显示 N 条（0=全部）")
    parser.add_argument("--guest", type=str, default="", help="按嘉宾名称过滤")
    parser.add_argument("--status", type=str, default="pending", help="过滤 status（pending/done/error/all）")
    args = parser.parse_args()

    if not MANIFEST_FILE.exists():
        print("❌ patch_manifest.json 不存在，请先运行 build_patch_manifest.py")
        return

    patches = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

    # 过滤
    if args.status != "all":
        patches = [p for p in patches if p["status"] == args.status]
    if args.guest:
        patches = [p for p in patches if args.guest.lower() in p["guest_name"].lower()]

    total = len(patches)
    if args.n > 0:
        patches = patches[: args.n]

    print(f"共 {total} 条待处理（显示 {len(patches)} 条）\n")

    for i, patch in enumerate(patches, 1):
        print(f"{'='*60}")
        print(f"[{i}/{len(patches)}] 视频 ID：{patch['video_id']}")
        print(f"  嘉宾：{patch['guest_name']}")
        print(f"  链接：https://www.youtube.com/watch?v={patch['video_id']}")
        show_diff(patch["old_description"], patch["new_description"])
        print()

    print(f"\n下一步：python3 tools/youtube/apply_patches.py --dry-run（模拟 API 调用）")
    print(f"正式写入：python3 tools/youtube/apply_patches.py")


if __name__ == "__main__":
    main()
