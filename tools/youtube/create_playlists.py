#!/usr/bin/env python3
"""
根据 playlist_manifest.json，在 YouTube 上创建/更新 playlist。

模式：
  --dry-run   只打印将要执行的操作，不实际调用 API
  --create    创建所有不存在的 playlist（10 units/个）
  --populate  往 playlist 里添加视频（50 units/个 video.update 操作）

注意：YouTube API 的 playlistItems.insert 消耗 50 units/次，
      330个视频 × 50 = 16,500 units，需要分2天执行。

用法：
  envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --dry-run
  envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --create
  envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --populate --dry-run
  envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --populate
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
MANIFEST_FILE = PROJECT_ROOT / "tools/youtube/playlist_manifest.json"
PLAYLIST_IDS_FILE = PROJECT_ROOT / "tools/youtube/playlist_ids.json"
POPULATE_PROGRESS_FILE = PROJECT_ROOT / "tools/youtube/populate_progress.json"

# ── 目标 Playlist 定义 ──────────────────────────────────────────────────────
PLAYLISTS = {
    "AI时代机会": {
        "title": "AI时代机会 | 趋势·职业·定位",
        "description": (
            "AI大趋势、AGI、AI对行业/职业的冲击、如何在AI时代定位自己——"
            "从一线视角解读科技变革，抓住时代机会。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["AI", "人工智能", "AGI", "大模型", "ChatGPT", "AI时代"],
    },
    "AI工具实战": {
        "title": "AI工具实战 | Cursor·Vibe Coding·工作流",
        "description": (
            "Cursor、Vibe Coding、具体AI工具、用AI替代工作流——"
            "用AI提升效率的实战指南。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["Cursor", "Vibe Coding", "AI工具", "prompt", "AI效率", "ChatGPT"],
    },
    "求职与面试": {
        "title": "求职与面试 | DS·PM·Eng面试全攻略",
        "description": (
            "求职技巧、简历、面试准备、DS/PM/Eng面试、New Grad找工作、跳槽策略——"
            "从投递到拿offer的完整指南。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["面试", "求职", "数据科学面试", "FAANG", "找工作", "简历"],
    },
    "职场技能": {
        "title": "职场技能 | 升职·汇报·沟通",
        "description": (
            "升职加薪的具体方法、向上汇报、沟通技巧、情绪价值、PPT表达、绩效谈判——"
            "可操作的职场软硬技能。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["升职", "汇报", "沟通", "职场技能", "绩效", "职场"],
    },
    "大厂观察": {
        "title": "大厂观察 | FAANG内幕·VP路径·裁员",
        "description": (
            "FAANG大厂内幕、VP路径分析、大厂vs Startup、裁员经历、公司衰落分析——"
            "了解大厂生态的第一手视角。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["FAANG", "大厂", "Meta", "Google", "裁员", "职业发展"],
    },
    "创业实战": {
        "title": "创业实战 | 创始人访谈·融资·产品",
        "description": (
            "从0到1创业故事、融资经历、产品市场fit、ToB/ToC、副业变现——"
            "创始人的第一手经验分享。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["创业", "startup", "融资", "创始人", "产品", "副业"],
    },
    "财富与投资": {
        "title": "财富与投资 | 从打工到财富自由",
        "description": (
            "个人理财、投资策略、财富自由路径、股票/加密/房产——"
            "摆脱韭菜思维，建立长期财富认知。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["财富自由", "投资", "理财", "股票", "加密货币", "FIRE"],
    },
    "思维与决策": {
        "title": "思维与决策 | 思维模型·判断力·认知升级",
        "description": (
            "思维模型、决策框架、批判性思维、认知偏差、独立思考——"
            "如何想得更清楚、做更好决策。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["思维模型", "决策", "认知", "批判性思维", "判断力", "独立思考"],
    },
    "人生设计": {
        "title": "人生设计 | 选择·自洽·意义感",
        "description": (
            "人生哲学、自洽、内耗、自我认知、重大选择（回国/离职/转型）、意义感——"
            "如何活得更好。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["人生设计", "自洽", "意义", "回国", "价值观", "人生选择"],
    },
    "数据科学": {
        "title": "数据科学 | DS·ML·AB实验",
        "description": (
            "DS/ML技术、AB实验、数据分析、数据科学家职业路径、分析师技能——"
            "数据从业者的专业成长指南。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["数据科学", "机器学习", "AB实验", "数据分析", "Data Scientist"],
    },
    "美国生活": {
        "title": "美国生活 | 文化·约会·回国vs留美",
        "description": (
            "生活记录、文化观察、约会婚恋、回国vs留美、美食、户外——"
            "在美华人的真实日常。\n\n更多内容：https://www.lizheng.ai"
        ),
        "tags": ["美国生活", "华人", "西雅图", "留学", "回国", "文化观察"],
    },
}


def load_manifest() -> dict:
    if not MANIFEST_FILE.exists():
        print(f"❌ {MANIFEST_FILE} 不存在，请先运行 classify_playlists.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))


def load_playlist_ids() -> dict:
    if PLAYLIST_IDS_FILE.exists():
        return json.loads(PLAYLIST_IDS_FILE.read_text(encoding="utf-8"))
    return {}


def save_playlist_ids(ids: dict):
    PLAYLIST_IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=2))


def group_by_playlist(manifest: dict) -> dict[str, list[str]]:
    """Returns {playlist_name: [video_id, ...]} sorted by view count desc."""
    groups = defaultdict(list)
    # Sort manifest by view_count desc so popular videos appear first
    sorted_vids = sorted(manifest.items(), key=lambda x: x[1].get("view_count", 0), reverse=True)
    for vid_id, info in sorted_vids:
        for cat in info.get("categories", []):
            if cat != "其他":
                groups[cat].append(vid_id)
    return dict(groups)


def print_plan(groups: dict):
    """Print what playlists will be created and how many videos each has."""
    print("\n=== Playlist 分类方案 ===\n")
    total_units = 0
    for cat, playlist_def in PLAYLISTS.items():
        vids = groups.get(cat, [])
        units = len(vids) * 50  # playlistItems.insert = 50 units each
        total_units += units
        print(f"  【{cat}】")
        print(f"    标题：{playlist_def['title']}")
        print(f"    视频数：{len(vids)}  预计配额：{units:,} units")
        print()

    other_vids = sum(1 for info in json.loads(MANIFEST_FILE.read_text()).values()
                     if "其他" in info.get("categories", []) or not info.get("categories"))
    print(f"  未分类（其他/边角料）：{other_vids} 个视频（不加入任何 playlist）")
    print(f"\n  总配额消耗：{total_units:,} units（需要分 {(total_units // 10000) + 1} 天执行）")
    print(f"  +10 units/playlist（创建）= {len(PLAYLISTS) * 10} units")


def create_playlists(youtube, dry_run: bool = True):
    """Create playlists that don't exist yet."""
    playlist_ids = load_playlist_ids()

    for cat, playlist_def in PLAYLISTS.items():
        if cat in playlist_ids:
            print(f"  ✓ {cat} 已存在：{playlist_ids[cat]}")
            continue

        if dry_run:
            print(f"  [DRY] 创建 playlist：{playlist_def['title']}")
            continue

        try:
            body = {
                "snippet": {
                    "title": playlist_def["title"],
                    "description": playlist_def["description"],
                    "defaultLanguage": "zh",
                    "tags": playlist_def.get("tags", []),
                },
                "status": {"privacyStatus": "public"},
            }
            resp = youtube.playlists().insert(part="snippet,status", body=body).execute()
            pl_id = resp["id"]
            playlist_ids[cat] = pl_id
            save_playlist_ids(playlist_ids)
            print(f"  ✓ 创建：{playlist_def['title']} → {pl_id}")
            time.sleep(0.5)
        except Exception as e:
            print(f"  ❌ 创建失败 {cat}: {e}")


def load_progress() -> dict:
    """Load set of already-inserted (cat, video_id) pairs."""
    if POPULATE_PROGRESS_FILE.exists():
        return json.loads(POPULATE_PROGRESS_FILE.read_text(encoding="utf-8"))
    return {}


def save_progress(progress: dict):
    POPULATE_PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2))


def populate_playlists(youtube, groups: dict, dry_run: bool = True, limit: int = 0):
    """Add videos to playlists. Skips already-inserted pairs. limit=0 means no limit."""
    playlist_ids = load_playlist_ids()
    progress = load_progress()  # {cat: [video_id, ...]}

    total_done = 0
    total_skip = 0
    total_err = 0
    total_limit = limit  # 0 = unlimited

    for cat, video_ids in groups.items():
        pl_id = playlist_ids.get(cat)
        if not pl_id:
            print(f"  ⚠ {cat} 没有 playlist ID，请先运行 --create")
            continue

        already_done = set(progress.get(cat, []))
        pending = [v for v in video_ids if v not in already_done]
        total_skip += len(already_done)

        print(f"\n  【{cat}】{len(pending)} 个待添加（{len(already_done)} 已完成）→ {pl_id}")

        if not pending:
            continue

        for vid_id in pending:
            if dry_run:
                print(f"    [DRY] 添加 {vid_id}")
                continue

            # Check global limit
            if total_limit and total_done >= total_limit:
                save_progress(progress)
                print(f"\n  ⏸ 已达到 --limit {total_limit}，停止。明天继续运行 --populate")
                print(f"✓ 今日：{total_done} 成功，{total_err} 失败，{total_skip} 跳过（已完成）")
                return

            try:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": pl_id,
                            "resourceId": {"kind": "youtube#video", "videoId": vid_id},
                        }
                    },
                ).execute()
                progress.setdefault(cat, []).append(vid_id)
                total_done += 1
                if total_done % 20 == 0:
                    save_progress(progress)
                    print(f"    [{total_done}] 已添加，进度已保存...")
                time.sleep(0.3)
            except Exception as e:
                err_str = str(e)
                if "duplicate" in err_str.lower() or "already" in err_str.lower():
                    progress.setdefault(cat, []).append(vid_id)
                    total_done += 1
                elif "quotaExceeded" in err_str or "quota" in err_str.lower():
                    save_progress(progress)
                    print(f"    ⚠ 配额耗尽（已完成 {total_done} 个）。明天继续运行 --populate")
                    print(f"✓ 今日：{total_done} 成功，{total_err} 失败，{total_skip} 跳过（已完成）")
                    return
                else:
                    print(f"    ❌ {vid_id}: {e}")
                    total_err += 1

        if not dry_run:
            save_progress(progress)
            print(f"    → 完成 {len(pending)} 个")

    if not dry_run:
        save_progress(progress)
        print(f"\n✓ 全部完成：{total_done} 成功，{total_err} 失败，{total_skip} 跳过")


def main():
    parser = argparse.ArgumentParser(description="创建 YouTube Playlist 并填充视频")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不实际操作")
    parser.add_argument("--create", action="store_true", help="创建 playlist")
    parser.add_argument("--populate", action="store_true", help="往 playlist 添加视频")
    parser.add_argument("--plan", action="store_true", help="只显示分类方案")
    parser.add_argument("--limit", type=int, default=0, help="populate 最多添加 N 个（按配额分天执行）")
    args = parser.parse_args()

    manifest = load_manifest()
    groups = group_by_playlist(manifest)

    if args.plan or not (args.create or args.populate):
        print_plan(groups)
        return

    # Need YouTube API
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "tools/youtube"))
    from auth import get_youtube_client
    youtube = get_youtube_client()

    if args.create:
        print("创建 Playlist...")
        create_playlists(youtube, dry_run=args.dry_run)

    if args.populate:
        print("填充视频...")
        populate_playlists(youtube, groups, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
