#!/usr/bin/env python3
"""
批量更新 YouTube 视频描述

工作流：
  1. 读取 patch_manifest.json（由 build_patch_manifest.py 生成）
  2. 批量 videos.list（50个/call, 1 unit/call）预取所有 snippet
  3. 对每个 pending 条目调用 videos.update（50 units/call）
  4. 断点续传：已标记 done 的自动跳过

配额消耗：
  - 读取：⌈N/50⌉ units（批量，极省）
  - 更新：N × 50 units
  - 353 个视频 ≈ 17,658 units，分 2 天跑（默认配额 10,000/天）

用法：
    python3 tools/youtube/apply_patches.py              # 正式写入
    python3 tools/youtube/apply_patches.py --dry-run   # 模拟，不实际调用 API
    python3 tools/youtube/apply_patches.py --limit 10  # 只处理前 10 条
    python3 tools/youtube/apply_patches.py --guest Leon # 只处理指定嘉宾
"""

import argparse
import json
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
MANIFEST_FILE = PROJECT_ROOT / "tools/youtube/patch_manifest.json"

# 每次 API 调用之间的间隔（秒），避免 rate limit
REQUEST_DELAY = 1.0


def load_manifest() -> list[dict]:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError("patch_manifest.json 不存在，请先运行 build_patch_manifest.py")
    return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))


def save_manifest(patches: list[dict]):
    MANIFEST_FILE.write_text(json.dumps(patches, ensure_ascii=False, indent=2), encoding="utf-8")


def batch_fetch_snippets(youtube, video_ids: list[str]) -> dict[str, dict]:
    """批量获取 snippet，50个/call，返回 {video_id: snippet}。"""
    snippets = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        resp = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            snippets[item["id"]] = item["snippet"]
    return snippets


def update_video_description(youtube, video_id: str, snippet: dict, new_description: str) -> bool:
    """用新描述更新视频 snippet。返回是否成功。"""
    snippet["description"] = new_description
    youtube.videos().update(
        part="snippet",
        body={"id": video_id, "snippet": snippet},
    ).execute()
    return True


def main():
    parser = argparse.ArgumentParser(description="批量更新 YouTube 视频描述")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不实际调用 API")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全部）")
    parser.add_argument("--guest", type=str, default="", help="只处理指定嘉宾（名称模糊匹配）")
    args = parser.parse_args()

    patches = load_manifest()

    # 筛选待处理条目
    targets = [p for p in patches if p["status"] == "pending"]
    if args.guest:
        targets = [p for p in targets if args.guest.lower() in p["guest_name"].lower()]
    if args.limit > 0:
        targets = targets[: args.limit]

    total = len(targets)
    done_count = sum(1 for p in patches if p["status"] == "done")
    print(f"manifest 总计：{len(patches)} 条")
    print(f"  已完成：{done_count}，本次待处理：{total}")
    if args.dry_run:
        print("  ⚡ DRY-RUN 模式，不实际写入 YouTube\n")

    if total == 0:
        print("没有需要处理的条目。")
        return

    # 只在非 dry-run 时才导入 YouTube client
    youtube = None
    snippets = {}
    if not args.dry_run:
        from tools.youtube.auth import get_youtube_client  # noqa: E402
        youtube = get_youtube_client()
        print("批量预取 snippet...", end=" ", flush=True)
        snippets = batch_fetch_snippets(youtube, [p["video_id"] for p in targets])
        print(f"获取 {len(snippets)}/{total} 条\n")

    success = 0
    errors = 0

    for i, patch in enumerate(targets, 1):
        vid = patch["video_id"]
        guest = patch["guest_name"]
        print(f"[{i}/{total}] {vid}  {guest}", end="  ", flush=True)

        if args.dry_run:
            print("✓ (dry-run)")
            success += 1
            continue

        try:
            snippet = snippets.get(vid)
            if snippet is None:
                raise ValueError("视频不存在或无权访问（可能是私密/删除）")

            update_video_description(youtube, vid, snippet, patch["new_description"])

            # 更新 manifest 状态
            for p in patches:
                if p["video_id"] == vid and p["guest_name"] == guest:
                    p["status"] = "done"
                    break
            save_manifest(patches)

            print("✓")
            success += 1

        except Exception as e:
            print(f"✗  {e}")
            for p in patches:
                if p["video_id"] == vid and p["guest_name"] == guest:
                    p["status"] = "error"
                    p["error"] = str(e)
                    break
            save_manifest(patches)
            errors += 1

        time.sleep(REQUEST_DELAY)

    print(f"\n完成：{success} 成功，{errors} 失败")
    if errors:
        print("失败的条目已标记为 error，可单独重试。")
    print(f"manifest 已更新：{MANIFEST_FILE}")


if __name__ == "__main__":
    # 支持直接运行（不作为包）
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    main()
