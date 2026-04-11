#!/usr/bin/env python3
"""
基于视频标题+描述，用 Claude API 对所有公开视频做 playlist 分类。

分类体系（6个主题 playlist + 自动生成「频道精选」）：
  AI与科技前沿 / 硅谷职场攻略 / 财富与投资 / 创业实战 / 认知与个人成长 / 硅谷生活

用法：
  envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py              # 全量分类
  envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --resume     # 断点续跑（跳过已有分类）
  envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --reclassify # 重分类「精选深度访谈/其他/产品增长」
  envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --stats      # 只看统计
  envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --limit N    # 测试（只跑N个）
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import anthropic

PROJECT_ROOT = Path(__file__).parent.parent.parent
ALL_VIDEOS_FILE = PROJECT_ROOT / "tools/youtube/all_videos_full.json"
MANIFEST_FILE = PROJECT_ROOT / "tools/youtube/playlist_manifest.json"

# 旧分类（触发重分类）
STALE_CATEGORIES = {
    "精选深度访谈", "产品增长",
    "AI与科技前沿", "硅谷职场攻略", "认知与个人成长",
    "职场晋升与领导力",  # 拆分为 职场技能 + 大厂观察
}

# 最终 11 个有效分类
VALID_CATEGORIES = {
    "AI时代机会",
    "AI工具实战",
    "求职与面试",
    "职场技能",
    "大厂观察",
    "创业实战",
    "财富与投资",
    "思维与决策",
    "人生设计",
    "数据科学",
    "美国生活",
}

SYSTEM_PROMPT = """你是帮助 YouTube 频道「课代表立正」做 playlist 分类的助手。
频道面向华人职场人和创业者（西雅图/湾区视角）。受众：25-40 岁高学历职场人和创业者。

将视频分配到以下 11 个 playlist（最多 2 个，优先选 1 个最精准的）：

1. AI时代机会
   ✓ AI大趋势、AGI、AI对行业/职业的冲击、如何在AI时代定位自己、AI投资机会
   ✗ 不含具体工具操作

2. AI工具实战
   ✓ Cursor/Vibe Coding/具体AI工具/用AI替代工作流/AI产品构建/prompt技巧

3. 求职与面试
   ✓ 求职技巧/简历/面试准备/DS-PM-Eng面试/New Grad找工作/跳槽策略
   ✗ 不含升职/管理

4. 职场技能
   ✓ 升职加薪的具体方法/向上汇报/沟通技巧/情绪价值/PPT表达/绩效谈判/职场人际
   注意：可操作的职场软硬技能，适合想提升具体能力的人

5. 大厂观察
   ✓ FAANG大厂内幕/VP路径分析/大厂vs Startup/裁员经历/大厂文化/公司衰落分析/管理层故事
   注意：对大公司的观察与分析，适合想了解大厂生态的人

6. 创业实战
   ✓ 创业故事/融资/产品市场fit/ToB/ToC/创始人访谈/副业变现/自媒体经营

7. 财富与投资
   ✓ 个人理财/投资策略/财富自由/股票/加密/房产/第一桶金/复利/宏观经济

8. 思维与决策
   ✓ 思维模型/决策框架/批判性思维/判断力/认知偏差/独立思考/信息筛选
   注意：侧重「工具性」——如何想得更清楚、做更好决策

9. 人生设计
   ✓ 人生哲学/自洽/内耗/情绪/自我认知/重大选择（回国/离职/转型）/意义感/价值观
   注意：侧重「内在探索」——如何活得更好

10. 数据科学
    ✓ DS/ML技术/AB实验/数据分析/数据科学家职业/分析师技能

11. 美国生活
    ✓ 生活记录/文化观察/约会婚恋/回国vs留美/美食/户外/育儿
    ✗ 不含以职业/财富/AI为核心的内容

「其他」仅用于：宠物/直播录像/婚礼片段/测试内容/幕后花絮/纯娱乐clips

判断提示：
- 「财富自由」访谈 → 财富与投资（不是人生设计）
- 「如何汇报/向上沟通/升职」→ 职场技能
- 「大厂 VP路径/大厂内幕/裁员故事」→ 大厂观察
- 「如何找到人生意义/自洽」→ 人生设计
- 「AI如何影响职业」→ AI时代机会
- 「离职故事/人生转折」→ 人生设计（可兼 大厂观察）
- 「数据科学面试」→ 数据科学 + 求职与面试

只返回 JSON：{"categories": ["分类名"], "reason": "一句话"}，不要其他文字"""


def load_videos() -> list[dict]:
    raw = json.loads(ALL_VIDEOS_FILE.read_text(encoding="utf-8"))
    seen = set()
    result = []
    for v in raw:
        if v["privacy"] == "public" and v["video_id"] not in seen:
            seen.add(v["video_id"])
            result.append(v)
    return result


def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict):
    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def classify_video(client: anthropic.Anthropic, video: dict) -> tuple[list[str], str]:
    title = video["title"]
    desc = (video.get("description") or "")[:400].strip()
    user_msg = f"标题：{title}\n描述（前400字）：{desc if desc else '（无描述）'}"

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        cats = data.get("categories", ["其他"])
        # Validate: remove any invalid categories
        cats = [c for c in cats if c in VALID_CATEGORIES or c == "其他"]
        if not cats:
            cats = ["其他"]
        return cats, data.get("reason", "")
    except Exception as e:
        print(f"  ⚠ 分类失败 {video['video_id']}: {e}", file=sys.stderr)
        return ["其他"], str(e)


def needs_reclassify(info: dict) -> bool:
    """Returns True if this entry should be re-classified."""
    cats = set(info.get("categories", []))
    # Any stale category → reclassify
    if cats & STALE_CATEGORIES:
        return True
    # Only "其他" → reclassify (give it another chance)
    if cats == {"其他"}:
        return True
    return False


def print_stats(manifest: dict, videos: list[dict]):
    vid_map = {v["video_id"]: v for v in videos}
    cat_counts = Counter()
    cat_views = defaultdict(int)

    for vid_id, info in manifest.items():
        cats = info.get("categories", [])
        v = vid_map.get(vid_id, {})
        views = v.get("view_count", 0)
        for c in cats:
            cat_counts[c] += 1
            cat_views[c] += views

    print("\n=== Playlist 分类统计 ===")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<16}  {count:>4} 个  {cat_views[cat]/1_000_000:.2f}M views")

    total = len(manifest)
    valid_total = sum(1 for info in manifest.values()
                      if set(info.get("categories", [])) - {"其他"})
    print(f"\n已分类（有效）：{valid_total} / {len(videos)} 个公开视频")
    print(f"待分类/其他：{total - valid_total}")

    # Show top 5 per category
    by_cat = defaultdict(list)
    for vid_id, info in manifest.items():
        for cat in info.get("categories", []):
            v = vid_map.get(vid_id, {})
            by_cat[cat].append((v.get("view_count", 0), info.get("title", ""), vid_id))

    print("\n=== 各分类 Top 5 ===")
    for cat in sorted(VALID_CATEGORIES):
        items = sorted(by_cat.get(cat, []), reverse=True)[:5]
        print(f"\n【{cat}】{len(by_cat.get(cat, []))} 个视频")
        for views, title, _ in items:
            print(f"  {views:>7,}  {title[:55]}")

    # 频道精选 preview
    all_items = []
    for vid_id, info in manifest.items():
        cats = info.get("categories", [])
        if cats and cats != ["其他"]:
            v = vid_map.get(vid_id, {})
            all_items.append((v.get("view_count", 0), info.get("title", ""), vid_id))
    all_items.sort(reverse=True)
    print(f"\n【频道精选（自动生成 Top 40）】")
    for views, title, _ in all_items[:10]:
        print(f"  {views:>7,}  {title[:55]}")
    print(f"  ... 共 {min(40, len(all_items))} 个")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="跳过已有有效分类的视频")
    parser.add_argument("--reclassify", action="store_true", help="重分类过时/错误分类的视频")
    parser.add_argument("--pass2", action="store_true", help="第二轮：重分类「其他」+ 高播放仅认知视频")
    parser.add_argument("--stats", action="store_true", help="只显示统计")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个（测试用）")
    args = parser.parse_args()

    videos = load_videos()
    manifest = load_manifest()

    if args.stats:
        print_stats(manifest, videos)
        return

    # Determine which videos to classify
    if args.pass2:
        # Re-classify "其他" + exclusively-认知 videos with high views
        targets = set()
        for v in videos:
            info = manifest.get(v["video_id"], {})
            cats = set(info.get("categories", []))
            views = info.get("view_count", 0)
            if cats == {"其他"}:
                targets.add(v["video_id"])
            elif cats == {"认知与个人成长"} and views > 15000:
                targets.add(v["video_id"])
        to_classify = [v for v in videos if v["video_id"] in targets]
        print(f"Pass 2 模式：{len(to_classify)} 个视频（其他 + 高播放仅认知）")
    elif args.resume:
        to_classify = [
            v for v in videos
            if v["video_id"] not in manifest
            or needs_reclassify(manifest[v["video_id"]])
        ]
        print(f"Resume 模式：{len(to_classify)} 个视频需要处理")
    elif args.reclassify:
        to_classify = [
            v for v in videos
            if v["video_id"] in manifest and needs_reclassify(manifest[v["video_id"]])
        ]
        print(f"Reclassify 模式：{len(to_classify)} 个需要重分类")
    else:
        to_classify = videos
        print(f"全量模式：{len(to_classify)} 个视频")

    if args.limit:
        to_classify = to_classify[: args.limit]

    if not to_classify:
        print("没有需要分类的视频。")
        print_stats(manifest, videos)
        return

    # Load API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        print("❌ ANTHROPIC_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print(f"开始分类 {len(to_classify)} 个视频...")
    batch_size = 20

    for i, video in enumerate(to_classify, 1):
        cats, reason = classify_video(client, video)

        # If reclassifying a multi-category entry, preserve the non-stale part
        existing = manifest.get(video["video_id"], {})
        if args.reclassify and existing:
            existing_cats = set(existing.get("categories", []))
            kept = existing_cats - STALE_CATEGORIES - {"其他"}
            if kept and cats == ["其他"]:
                # New classification failed but we had valid cats → keep existing valid ones
                cats = list(kept)
                reason = "(kept existing valid classification)"

        manifest[video["video_id"]] = {
            "title": video["title"],
            "categories": cats,
            "reason": reason,
            "view_count": video.get("view_count", 0),
        }

        if i % batch_size == 0:
            save_manifest(manifest)
            print(f"  [{i}/{len(to_classify)}] 已保存...")

        time.sleep(0.05)

    save_manifest(manifest)
    print(f"\n✓ 完成，结果写入：{MANIFEST_FILE}")
    print_stats(manifest, videos)


if __name__ == "__main__":
    main()
